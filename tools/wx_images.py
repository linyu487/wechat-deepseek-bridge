"""微信 4.x 图片 .dat 解密（V2: AES-128-ECB + XOR）。

密钥优先按微信的命名约定从 uin + wxid 派生（Windows kvcomm 缓存里有 uin），
失败再退回进程内存扫描。结果缓存在 tmp/image-key.json。
"""
import ctypes
import glob
import hashlib
import json
import os
import re
import struct
import subprocess
import sys

from Crypto.Cipher import AES
from Crypto.Util import Padding

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
KEY_CACHE = os.path.join(TMP, "image-key.json")
OUT_DIR = os.path.join(TMP, "wx-images")

BASE = r"CHANGE_ME_WX_DATA_DIR"  # 去掉 \db_storage 的上一级
ATTACH = os.path.join(BASE, "msg", "attach")
SELF_WXID = "CHANGE_ME_SELF_WXID"
KVCOMM_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat", "net", "kvcomm"),
    os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat", "ilink", "kvcomm"),
]

V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
MAGIC_SETS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG", "png"),
    (b"RIFF", "webp"),
    (b"GIF8", "gif"),
    (b"wxgf", "hevc"),
]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

RE32 = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")
RE16 = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16}(?![A-Za-z0-9])")
REHEX32 = re.compile(rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
    ]


kernel32 = ctypes.windll.kernel32


def wechat_pids():
    out = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    pids = []
    for line in out.strip().splitlines():
        parts = line.strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower().startswith(("weixin.exe", "wechatappex.exe")):
            if parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def newest_v2_dats(n=8):
    files = []
    for p in glob.glob(os.path.join(ATTACH, "*", "*", "Img", "*.dat")):
        try:
            with open(p, "rb") as f:
                if f.read(6) == V2_MAGIC:
                    files.append(p)
        except OSError:
            pass
    files.sort(key=os.path.getmtime, reverse=True)
    return files[:n]


def xor_candidates(dat):
    out = [0x88]
    for p in [dat] + newest_v2_dats(8):
        if p:
            out.append(xor_key_from(p))
    seen = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def find_v2_dat():
    files = newest_v2_dats(1)
    return files[0] if files else None


def xor_key_from(dat_path):
    try:
        size = os.path.getsize(dat_path)
        with open(dat_path, "rb") as f:
            f.seek(size - 2)
            t = f.read(2)
        if len(t) == 2 and (t[0] ^ 0xFF) == (t[1] ^ 0xD9):
            return t[0] ^ 0xFF
    except OSError:
        pass
    return 0x88


def fmt_of(block):
    for magic, name in MAGIC_SETS:
        if block.startswith(magic):
            return name
    return None


def try_key(key, cipher_block):
    try:
        dec = AES.new(key, AES.MODE_ECB).decrypt(cipher_block)
    except Exception:
        return None
    return fmt_of(dec)


def scan_key(pid, cipher_block, dat_path, xors):
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None, None
    try:
        regions = []
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                       ctypes.sizeof(mbi)) == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.Protect != PAGE_NOACCESS
                    and not (mbi.Protect & PAGE_GUARD) and mbi.RegionSize <= 50 * 1024 * 1024):
                regions.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect))
            nxt = addr + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

        rw = (0x04, 0x08, 0x40, 0x80)
        ordered = ([r for r in regions if (r[2] & sum(rw)) != 0] +
                   [r for r in regions if (r[2] & sum(rw)) == 0])

        def verify(candidate):
            for x in xors:
                out, fmt = decrypt_dat(dat_path, candidate, x)
                if out is not None:
                    return x
            return None

        for base, size, _protect in ordered:
            buf = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                              ctypes.byref(read)) or read.value < 32:
                continue
            data = buf.raw[:read.value]
            for m in REHEX32.finditer(data):
                try:
                    kb = bytes.fromhex(m.group().decode("ascii"))
                except ValueError:
                    continue
                if try_key(kb, cipher_block):
                    x = verify(kb)
                    if x is not None:
                        return kb, x
            for m in RE32.finditer(data):
                kb = m.group()
                if try_key(kb[:16], cipher_block):
                    x = verify(kb[:16].decode("ascii"))
                    if x is not None:
                        return kb[:16].decode("ascii"), x
                if try_key(kb, cipher_block):
                    x = verify(kb.decode("ascii"))
                    if x is not None:
                        return kb.decode("ascii"), x
            for m in RE16.finditer(data):
                kb = m.group()
                if try_key(kb, cipher_block):
                    x = verify(kb.decode("ascii"))
                    if x is not None:
                        return kb.decode("ascii"), x
    finally:
        kernel32.CloseHandle(h)
    return None, None


def load_cached():
    try:
        with open(KEY_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if "aes_hex" in d:
            return bytes.fromhex(d["aes_hex"]), int(d.get("xor", 0x88))
        return d.get("aes"), int(d.get("xor", 0x88))
    except Exception:
        return None, 0x88


def save_cached(aes, xor):
    os.makedirs(TMP, exist_ok=True)
    rec = {"xor": xor}
    if isinstance(aes, bytes):
        rec["aes_hex"] = aes.hex()
    else:
        rec["aes"] = aes
    with open(KEY_CACHE, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


def key_bytes(k):
    if isinstance(k, bytes):
        return k[:16]
    return k.encode("ascii")[:16]


def kvcomm_codes():
    codes = set()
    for d in KVCOMM_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            m = re.match(r"^key_(\d+)_.+\.statistic$", name, re.I)
            if m:
                codes.add(int(m.group(1)))
    return sorted(codes)


def derive_key(uin, wxid):
    return uin & 0xFF, hashlib.md5(f"{uin}{wxid}".encode()).hexdigest()[:16]


def ensure_key(force=False):
    aes, xor = load_cached()
    dat = find_v2_dat()
    if dat is None:
        return aes, xor
    with open(dat, "rb") as f:
        head = f.read(31)
    block = head[15:31]
    xors = xor_candidates(dat)
    if aes and not force and try_key(key_bytes(aes), block):
        for x in [xor] + xors:
            out, _ = decrypt_dat(dat, aes, x)
            if out is not None:
                return aes, x

    # 1) 按微信命名约定从 uin + wxid 派生（最快，且换会话也稳定）
    for uin in kvcomm_codes():
        for wxid in (SELF_WXID, SELF_WXID + "_1882"):
            x, k = derive_key(uin, wxid)
            if try_key(k.encode("ascii"), block):
                out, _ = decrypt_dat(dat, k, x)
                if out is not None:
                    save_cached(k, x)
                    return k, x

    # 2) 进程内存扫描兜底
    for pid in wechat_pids():
        k, x = scan_key(pid, block, dat, xors)
        if k:
            save_cached(k, x)
            return k, x
    return aes, xor


def xor_from_dat(dat):
    return xor_key_from(dat)


def aligned(aes_size):
    return aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16


def decrypt_dat(dat_path, aes_key, xor_key):
    with open(dat_path, "rb") as f:
        data = f.read()
    if len(data) < 15:
        return None, None
    sig = data[:6]
    if sig == V1_MAGIC:
        key = b"cfcd208495d565ef"
    elif sig == V2_MAGIC:
        if not aes_key:
            return None, None
        key = aes_key.encode("ascii")[:16] if isinstance(aes_key, str) else aes_key[:16]
    else:
        return None, None

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    n = aligned(aes_size)
    offset = 15
    if offset + n > len(data):
        return None, None
    try:
        dec = Padding.unpad(AES.new(key, AES.MODE_ECB).decrypt(data[offset:offset + n]),
                            AES.block_size)
    except Exception:
        return None, None
    offset += n
    raw_end = len(data) - xor_size
    raw = data[offset:raw_end] if offset < raw_end else b""
    xored = bytes(b ^ xor_key for b in data[raw_end:])
    out = dec + raw + xored
    fmt = fmt_of(out[:16])
    if fmt is None:
        return None, None
    if fmt == "jpg" and not out.endswith(b"\xff\xd9"):
        return None, None
    if fmt == "png" and b"IEND" not in out[-12:]:
        return None, None
    return out, fmt


def resolve(peer_wxid, file_md5, force=False):
    """按 (peer, 图片 md5) 找到 .dat 并解密，返回图片路径。"""
    import hashlib
    h = hashlib.md5(peer_wxid.encode()).hexdigest()
    base = os.path.join(ATTACH, h)
    cands = sorted(glob.glob(os.path.join(base, "*", "Img", f"{file_md5}*.dat")))
    if not cands:
        return None, "no-dat"
    # 优先原图（文件名就是 md5），其次 _h，最后 _t
    cands.sort(key=lambda p: (os.path.basename(p) != file_md5 + ".dat", not p.endswith("_h.dat")))
    aes, xor = ensure_key(force)
    last_err = None
    for c in cands:
        out, fmt = decrypt_dat(c, aes, xor)
        if out:
            os.makedirs(OUT_DIR, exist_ok=True)
            dst = os.path.join(OUT_DIR, f"{file_md5}.{fmt if fmt != 'hevc' else 'hevc'}")
            with open(dst, "wb") as f:
                f.write(out)
            return dst, fmt
        last_err = "decrypt-failed"
    return None, last_err


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--key":
        aes, xor = ensure_key(force=True)
        print(f"aes={aes} xor=0x{xor:02x}")
    elif len(sys.argv) > 2:
        path, fmt = resolve(sys.argv[1], sys.argv[2], force="--force" in sys.argv)
        print(path, fmt)
    else:
        dat = find_v2_dat()
        print("latest v2 dat:", dat)
        aes, xor = ensure_key(force=True)
        print(f"aes={aes} xor=0x{xor:02x}")
        if dat and aes:
            out, fmt = decrypt_dat(dat, aes, xor)
            print("decrypt:", fmt, len(out) if out else None)
