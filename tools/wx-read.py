"""读取小号微信的聊天记录（解密 + 应用 WAL）。

用法:
  python wx-read.py            # 最近 20 条
  python wx-read.py 50         # 最近 50 条

依赖:
  - tmp/keys.txt  由 tools/dump-keys.ps1 生成（salt:key 每行一条）
  - 微信 4.1.9 正在运行（密钥在内存里）
"""
import hashlib
import os
import sqlite3
import struct
import sys
import tempfile
import datetime
import zstandard
from Crypto.Cipher import AES

PAGE = 4096
RESERVE = 80
IV_OFF = PAGE - RESERVE

DB_DIR = r"CHANGE_ME_DB_DIR"  # 同上
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tmp", "keys.txt")
OUT_DIR = os.path.join(tempfile.gettempdir(), "wx-dec")


def load_keys():
    keys = {}
    with open(os.path.abspath(KEYS_FILE), encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                salt, key = line.split(":", 1)
                keys[salt.lower()] = bytes.fromhex(key)
    return keys


def dec_page(page, key, pgno):
    iv = page[IV_OFF:IV_OFF + 16]
    if pgno == 1:
        d = AES.new(key, AES.MODE_CBC, iv).decrypt(page[16:IV_OFF])
        return b"SQLite format 3\x00" + d + b"\x00" * RESERVE
    d = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:IV_OFF])
    return d + b"\x00" * RESERVE


def decrypt(src, key, dst):
    data = open(src, "rb").read()
    pages = {i + 1: dec_page(data[i * PAGE:(i + 1) * PAGE], key, i + 1)
             for i in range(len(data) // PAGE)}
    wal = src + "-wal"
    if os.path.exists(wal):
        with open(wal, "rb") as f:
            hdr = f.read(32)
            if len(hdr) == 32:
                salt = hdr[16:24]
                while True:
                    fr = f.read(24 + PAGE)
                    if len(fr) < 24 + PAGE:
                        break
                    if fr[8:16] != salt:
                        continue
                    pgno = int.from_bytes(fr[0:4], "big")
                    if 0 < pgno < 10_000_000:
                        pages[pgno] = dec_page(fr[24:24 + PAGE], key, pgno)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "wb").write(b"".join(pages[i] for i in sorted(pages)))


def text_of(content, flag, dctx):
    if not content:
        return ""
    if flag == 4:
        try:
            return dctx.decompress(content).decode("utf-8", "replace")
        except Exception:
            return "<zstd?>"
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    keys = load_keys()
    msg_dir = os.path.join(DB_DIR, "message")
    db_path = os.path.join(msg_dir, "message_0.db")
    salt = open(db_path, "rb").read(16).hex()
    key = keys.get(salt)
    if not key:
        print("没有这个库的密钥，先跑 tools/dump-keys.ps1")
        return

    dst = os.path.join(OUT_DIR, "message_0.db")
    decrypt(db_path, key, dst)

    con = sqlite3.connect(dst)
    names = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}

    # 联系人名
    contact_names = {}
    cdb = os.path.join(DB_DIR, "contact", "contact.db")
    try:
        csalt = open(cdb, "rb").read(16).hex()
        if csalt in keys:
            cdst = os.path.join(OUT_DIR, "contact.db")
            decrypt(cdb, keys[csalt], cdst)
            c = sqlite3.connect(cdst)
            for u, nick, remark in c.execute("SELECT username, nick_name, remark FROM contact"):
                contact_names[hashlib.md5(u.encode()).hexdigest()] = remark or nick or u
            c.close()
    except Exception:
        pass

    dctx = zstandard.ZstdDecompressor()
    rows = []
    for (tab,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
        h = tab[4:]
        who = contact_names.get(h, h)
        for lt, ct, sid, content, flag in con.execute(
                f"SELECT local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content FROM {tab}"):
            rows.append((ct, who, names.get(sid, sid), lt & 0xFFFF, text_of(content, flag, dctx)))
    rows.sort(key=lambda x: x[0])

    for ct, who, sender, lt, txt in rows[-limit:]:
        t = datetime.datetime.fromtimestamp(ct).strftime("%m-%d %H:%M:%S")
        txt = txt.replace("\n", " ")[:100]
        print(f"{t} [{who}] {sender}: {txt}")


if __name__ == "__main__":
    main()
