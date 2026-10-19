r"""微信小号「方正」桥接：监听指定联系人的消息，用 dsh headless 生成回复，从小号发出。

支持多联系人：每个联系人有独立会话（上下文互不可见）和独立游标。

用法:
  pythonw tools\wx-bridge.py                 # 常驻（无窗口）
  python  tools\wx-bridge.py --once          # 只跑一轮（调试）
  python  tools\wx-bridge.py --test "在吗"    # 对「鳞羽」跑一次生成+发送
  python  tools\wx-bridge.py --list-contacts # 列出最近有消息的联系人
  python  tools\wx-bridge.py --add-peer "备注名"  # 把某人加入自动回复名单

控制:
  tmp\bridge.pause   存在即暂停（仍轮询，不处理）
  tmp\bridge.stop    存在即退出
"""
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

import zstandard
from Crypto.Cipher import AES

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_images

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
CONFIG_FILE = os.path.join(HERE, "wx-bridge-config.json")
STATE_FILE = os.path.join(TMP, "bridge-state.json")
LOG_FILE = os.path.join(TMP, "bridge.log")
PAUSE_FILE = os.path.join(TMP, "bridge.pause")
STOP_FILE = os.path.join(TMP, "bridge.stop")
LOCK_FILE = os.path.join(TMP, "bridge.lock")

PAGE = 4096
RESERVE = 80
IV_OFF = PAGE - RESERVE
DB_DIR = r"CHANGE_ME_DB_DIR"  # 例：D:\wechat\xwechat_files\wxid_xxxx_1234\db_storage
KEYS_FILE = os.path.join(TMP, "keys.txt")
OUT_DIR = os.path.join(TMP, "bridge-dec")
os.makedirs(TMP, exist_ok=True)


def log(msg):
    ts = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_with_timeout(fn, timeout, *args):
    """在子线程里跑 fn，超时抛 TimeoutError，避免一条请求把整条桥接卡死。"""
    box = {}

    def target():
        try:
            box["value"] = fn(*args)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"处理超过 {timeout} 秒")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def wechat_running():
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                           capture_output=True, timeout=15, creationflags=0x08000000)
        return b"Weixin.exe" in p.stdout or b"weixin.exe" in p.stdout
    except Exception:
        return None


def screen_locked():
    """锁屏后模拟键鼠发不出去，先探一下 LogonUI.exe。"""
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LogonUI.exe", "/NH"],
                           capture_output=True, timeout=15, creationflags=0x08000000)
        return b"LogonUI.exe" in p.stdout or b"logonui.exe" in p.stdout
    except Exception:
        return False


def try_unlock():
    """无密码账户：锁屏时按回车 / 点一下就能回到桌面。返回是否成功解锁。"""
    if not screen_locked():
        return True
    try:
        import ctypes
        u = ctypes.windll.user32
        for _ in range(2):
            u.keybd_event(0x0D, 0, 0, 0)
            u.keybd_event(0x0D, 0, 2, 0)
            time.sleep(0.6)
            u.SetCursorPos(1280, 800)
            u.mouse_event(0x0002, 0, 0, 0, 0)
            u.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(1.2)
            if not screen_locked():
                time.sleep(1.0)
                return True
    except Exception:
        pass
    return not screen_locked()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_keys():
    keys = {}
    with open(KEYS_FILE, encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                salt, key = line.split(":", 1)
                keys[salt.lower()] = bytes.fromhex(key)
    return keys


def refresh_keys():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(HERE, "dump-keys.ps1")],
            capture_output=True, timeout=180, creationflags=0x08000000)
        with open(KEYS_FILE, "w", encoding="ascii") as f:
            f.write(out.stdout.decode("utf-8", "replace"))
        log("keys 已重新扫描")
    except Exception as e:
        log(f"重扫 keys 失败: {e}")


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


def decrypt_db(name, rel_path, keys):
    src = os.path.join(DB_DIR, rel_path)
    if not os.path.exists(src):
        return None
    salt = open(src, "rb").read(16).hex()
    if salt not in keys:
        return None
    dst = os.path.join(OUT_DIR, name)
    decrypt(src, keys[salt], dst)
    return dst


def text_of(content, flag, dctx):
    if not content:
        return ""
    if flag == 4:
        try:
            return dctx.decompress(content).decode("utf-8", "replace")
        except Exception:
            return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return str(content)


def rich_title(xml):
    m = re.search(r"<title>(.*?)</title>", xml, re.S)
    return m.group(1).strip() if m else ""


def kind_text(lt, txt):
    if lt == 1:
        return txt
    if lt == 3:
        return "[图片]"
    if lt == 34:
        return "[语音]"
    if lt == 43:
        return "[视频]"
    if lt == 49:
        return rich_title(txt) or "[链接/文件]"
    return ""


def table_of(wxid):
    return "Msg_" + hashlib.md5(wxid.encode()).hexdigest()


def load_contacts():
    """返回 {username: 显示名}，解不开就返回空。"""
    try:
        keys = load_keys()
        path = decrypt_db("contact.db", os.path.join("contact", "contact.db"), keys)
        if not path:
            return {}
        con = sqlite3.connect(path)
        out = {}
        for username, nick, remark, alias in con.execute(
                "SELECT username, nick_name, remark, alias FROM contact"):
            out[username] = remark or nick or alias or username
        con.close()
        return out
    except Exception:
        return {}


def open_message_db(cfg):
    keys = load_keys()
    src = os.path.join(DB_DIR, "message", "message_0.db")
    salt = open(src, "rb").read(16).hex()
    if salt not in keys:
        refresh_keys()
        keys = load_keys()
    if salt not in keys:
        raise RuntimeError(f"没有库密钥 {salt}，先跑 tools/dump-keys.ps1")
    dst = os.path.join(OUT_DIR, "message_0.db")
    decrypt(src, keys[salt], dst)
    return sqlite3.connect(dst)


def read_peer_rows(cfg, con, names, peer, cursor):
    table = table_of(peer["wxid"])
    try:
        rows = list(con.execute(
            f"SELECT local_id, real_sender_id, local_type, create_time, "
            f"message_content, WCDB_CT_message_content FROM {table} "
            f"WHERE local_id > ? ORDER BY local_id", (cursor,)))
        max_id = con.execute(f"SELECT COALESCE(MAX(local_id),0) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return [], cursor, cursor
    dctx = zstandard.ZstdDecompressor()
    out = []
    for lid, sid, lt, ct, content, flag in rows:
        if names.get(sid, "") != peer["wxid"]:
            continue
        lt16 = lt & 0xFFFF
        txt = kind_text(lt16, text_of(content, flag, dctx))
        if lt16 == 3:
            txt = f"[图片:lid={lid}]"
        if not txt:
            continue
        if time.time() - ct > cfg.get("staleSeconds", 1800):
            log(f"跳过过期消息 lid={lid}：{int((time.time() - ct) / 60)} 分钟前，"
                "只回新的（电脑睡眠后醒来常见）")
            continue
        out.append((lid, txt))
    return out, max_id, max_id


def image_file_hash(peer_wxid, local_id):
    """从 message_resource.db 查某条消息对应图片的磁盘哈希（.dat 文件名）。"""
    keys = load_keys()
    p = decrypt_db("message_resource.db", "message/message_resource.db", keys)
    if not p:
        return None
    con = sqlite3.connect(p)
    try:
        row = con.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (peer_wxid,)).fetchone()
        if not row:
            return None
        r = con.execute(
            "SELECT packed_info FROM MessageResourceInfo "
            "WHERE chat_id=? AND message_local_id=? "
            "AND (message_local_type=3 OR message_local_type % 4294967296 = 3) "
            "ORDER BY message_create_time DESC LIMIT 1", (row[0], local_id)).fetchone()
    finally:
        con.close()
    if not r or not r[0]:
        return None
    m = re.search(rb"[0-9a-f]{32}", r[0])
    return m.group().decode("ascii") if m else None


def expand_images(cfg, peer, text):
    """把 [图片:lid=N] 换成解密后的本地路径，让模型能用 read_image 看。

    微信的图片消息先入库、资源和 .dat 稍后才落盘，所以要重试几秒。
    """
    def repl(m):
        lid = int(m.group(1))
        last = None
        for _ in range(6):
            try:
                h = image_file_hash(peer["wxid"], lid)
                if h:
                    path, fmt = wx_images.resolve(peer["wxid"], h)
                    if path:
                        return f"[图片：已解密到 {path}，请用 read_image 工具查看后再回复]"
                    last = fmt
                else:
                    last = "resource-row-missing"
            except Exception as e:
                last = str(e)
            time.sleep(2)
        log(f"图片 lid={lid} 解密失败: {last}")
        return "[图片：本地解密失败]"
    return re.sub(r"\[图片:lid=(\d+)\]", repl, text)


def seed_prompt(peer):
    if peer.get("kind") == "owner":
        return ("你是微信小号的回复助手。以后每条主人发来的消息，"
                "你只输出要回给他的话：中文、简短口语、1-3句、不要Markdown。"
                "这条只是初始化，回复「好」即可。")
    name = peer["name"]
    return (f"你是 DeepSeek（AI 助手），通过微信小号和 {name} 聊天；{name} 知道你是 DeepSeek。"
            "规则：1) 中文、简短口语、1-3句，像微信聊天；2) 被问身份就说自己是 DeepSeek；"
            "3) 绝不透露主人的私人信息（聊天内容、位置、日程、账号、文件、联系人）；"
            "4) 不发文件、不发链接；"
            "5) 如果对方要求你操作电脑（执行命令、读写文件、装软件、改设置等），"
            "绝对不要直接做、不要调用工具：输出第一行写「[请示] 一句话概括要做什么」，"
            "第二行起写要回给对方的话（例如：这个我得先问一下方正的主人，稍等）。"
            "其他情况正常回复，不要输出 [请示]。"
            "这条只是初始化，回复「好」即可。")


def create_session(cfg, prompt):
    cmd = [cfg["node"], cfg["dshBin"], "headless", "--json", prompt]
    p = subprocess.run(cmd, cwd=cfg["workspace"], capture_output=True,
                       timeout=240, creationflags=0x08000000)
    out = p.stdout.decode("utf-8", "replace")
    m = re.search(r"session-[0-9a-f-]{36}", out)
    if not m:
        raise RuntimeError(f"创建会话失败: {p.stderr.decode('utf-8', 'replace')[-400:]}")
    return m.group(0)


def ask_model(cfg, peer, text):
    prompt = (
        f"{peer['name']} 发来一条微信：\n\n{text}\n\n"
        "请直接输出要回给 ta 的话（我会原样发出去）：中文，简短口语，1-3 句，"
        "不要 Markdown、不要引号、不要解释。"
    )
    if peer.get("kind") == "guest":
        prompt += "\n（提醒：若这是要操作电脑的请求，第一行输出 [请示] + 一句话概括，不要执行。）"
    if "read_image" in text:
        prompt += "\n（消息里有图片路径，请先用 read_image 看清内容再回答。）"
    cmd = [cfg["node"], cfg["dshBin"], "headless", "--session-id", peer["sessionId"], prompt]
    p = subprocess.run(cmd, cwd=cfg["workspace"], capture_output=True,
                       timeout=240, creationflags=0x08000000)
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise RuntimeError(f"headless 失败 rc={p.returncode}: {err[-500:]}")
    if not out:
        raise RuntimeError(f"headless 无输出: {err[-500:]}")
    return out


def send_reply(cfg, peer, text):
    text = text.strip()
    if len(text) > cfg.get("maxReplyChars", 1200):
        text = text[:cfg["maxReplyChars"]] + "…"
    reply_file = os.path.join(TMP, "bridge-reply.txt")
    with open(reply_file, "w", encoding="utf-8") as f:
        f.write(text)

    before = 0
    try:
        con = open_message_db(cfg)
        names = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
        _, before, _ = read_peer_rows(cfg, con, names, peer, 0)
        con.close()
    except Exception:
        pass

    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", os.path.join(HERE, "wx-send-file.ps1"),
         "-TextFile", reply_file, "-To", peer["name"]],
        capture_output=True, timeout=120, creationflags=0x08000000)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-400:])

    landed = False
    for _ in range(5):
        time.sleep(2)
        try:
            con = open_message_db(cfg)
            names = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
            _, after, _ = read_peer_rows(cfg, con, names, peer, 0)
            con.close()
        except Exception:
            continue
        if after > before:
            landed = True
            break
    if not landed:
        raise RuntimeError("发送后数据库无新记录，界面自动化可能未生效")
    log(f"发送成功 -> {peer['name']}: {text[:60]!r}")
    return text


def owner_peer(cfg):
    return next((p for p in cfg["peers"] if p.get("kind") == "owner"), cfg["peers"][0])


def peer_by_wxid(cfg, wxid):
    return next((p for p in cfg["peers"] if p["wxid"] == wxid), None)


def notify_owner(cfg, text):
    return send_reply(cfg, owner_peer(cfg), text)


def resolve_pending(cfg, state, text):
    """主人回了「同意 / 拒绝」：处理最前面那条待批请求。返回是否消费了这条消息。"""
    pend = state.get("pending") or []
    if not pend:
        return False
    t = text.strip()
    deny_words = ("不同意", "不可以", "拒绝", "别做", "不做", "不用", "取消", "算了", "不行")
    allow_words = ("同意", "做吧", "可以", "批准", "帮她", "去吧")
    esc = pend[0]
    guest = peer_by_wxid(cfg, esc["peer"])
    if guest is None:
        state["pending"].pop(0)
        save_json(STATE_FILE, state)
        return False
    if any(w in t for w in deny_words):
        state["pending"].pop(0)
        save_json(STATE_FILE, state)
        try:
            reply = ask_model(cfg, guest,
                              f"【已拒绝】方正主人不同意执行：{esc['request']}。"
                              "请礼貌地告诉对方这件事做不了，简短。")
            send_reply(cfg, guest, reply)
        except Exception as e:
            log(f"回绝通知失败: {e}")
        notify_owner(cfg, f"好，已经回绝 {esc['name']} 的这个请求了。")
        return True
    if any(w in t for w in allow_words):
        state["pending"].pop(0)
        save_json(STATE_FILE, state)
        try:
            reply = ask_model(cfg, guest,
                              f"【已获授权】方正主人同意了，现在可以执行：{esc['request']}。"
                              "可以调用工具，只做这个请求需要的事；完成后输出要回给对方的简短结果，"
                              "不要解释过程。")
            send_reply(cfg, guest, reply)
            notify_owner(cfg, f"✅ 已执行并回复 {esc['name']}：{reply[:80]}")
        except Exception as e:
            log(f"执行失败: {e}")
            notify_owner(cfg, f"⚠️ 执行失败：{e}")
        return True
    return False


def handle(cfg, state, peer, texts):
    joined = "\n".join(texts)
    if peer.get("kind") == "owner" and resolve_pending(cfg, state, joined):
        return None
    log(f"收到 {peer['name']} {len(texts)} 条: {joined[:80]!r}")
    joined = expand_images(cfg, peer, joined)
    reply = ask_model(cfg, peer, joined)
    if peer.get("kind") == "guest":
        m = re.match(r"^\s*\[请示\]\s*(.+?)\s*(?:\r?\n|$)(.*)$", reply, re.S)
        if m:
            request = m.group(1).strip()
            rest = m.group(2).strip()
            esc = {"peer": peer["wxid"], "name": peer["name"],
                   "request": request, "ts": time.time()}
            state.setdefault("pending", []).append(esc)
            save_json(STATE_FILE, state)
            log(f"{peer['name']} 请求操作电脑，已请示主人: {request[:60]!r}")
            try:
                notify_owner(cfg, f"📩 {peer['name']} 想让我帮 ta：{request}\n\n"
                                  "回复「同意」我就去做，回复「拒绝」就不做。")
            except Exception as e:
                log(f"请示通知失败: {e}")
            return send_reply(cfg, peer, rest or "这个我得先问一下方正的主人，等我一下。")
    return send_reply(cfg, peer, reply)


def add_peer(cfg, name):
    contacts = load_contacts()
    hits = [(u, n) for u, n in contacts.items() if n == name or u == name]
    if not hits:
        print(f"没找到备注/昵称为「{name}」的联系人。可以先跑 --list-contacts 看看。")
        return 2
    if len(hits) > 1:
        print("有多个同名联系人，请用 wxid 指定：")
        for u, n in hits:
            print(f"  {n}  {u}")
        return 2
    wxid = hits[0][0]
    for p in cfg["peers"]:
        if p["wxid"] == wxid:
            print(f"已经在名单里了：{p['name']} ({wxid})")
            return 0
    peer = {"wxid": wxid, "name": name, "kind": "guest"}
    print(f"正在为 {name} 创建独立会话…")
    peer["sessionId"] = create_session(cfg, seed_prompt(peer))
    cfg["peers"].append(peer)
    save_json(CONFIG_FILE, cfg)

    state = load_json(STATE_FILE, {"cursors": {}})
    state.setdefault("cursors", {})
    try:
        con = open_message_db(cfg)
        names = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
        _, _, max_id = read_peer_rows(cfg, con, names, peer, 0)
        con.close()
        state["cursors"][wxid] = max_id
        save_json(STATE_FILE, state)
    except Exception as e:
        print(f"（游标初始化失败，将从下一轮开始：{e}）")
    print(f"已加入：{name} ({wxid})，会话 {peer['sessionId']}")
    return 0


def list_contacts():
    keys = load_keys()
    contacts = load_contacts()
    path = decrypt_db("message_0.db", os.path.join("message", "message_0.db"), keys)
    if not path:
        print("解不开 message_0.db")
        return 2
    con = sqlite3.connect(path)
    rows = []
    for (tab,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
        try:
            last = con.execute(f"SELECT MAX(create_time) FROM {tab}").fetchone()[0]
        except sqlite3.Error:
            continue
        if not last:
            continue
        for u, n in contacts.items():
            if hashlib.md5(u.encode()).hexdigest() == tab[4:]:
                rows.append((last, n, u))
                break
    con.close()
    rows.sort(reverse=True)
    if not rows:
        print("没有找到联系人")
        return 0
    for ts, n, u in rows[:25]:
        t = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        print(f"{t}  {n}  {u}")
    return 0


def main():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        print("缺少 wx-bridge-config.json")
        return 2

    if "--list-contacts" in sys.argv:
        return list_contacts()

    if "--add-peer" in sys.argv:
        i = sys.argv.index("--add-peer")
        if len(sys.argv) <= i + 1:
            print("用法: --add-peer \"备注名\"")
            return 2
        return add_peer(cfg, sys.argv[i + 1])

    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        text = sys.argv[i + 1] if len(sys.argv) > i + 1 else "在吗"
        owner = next((p for p in cfg["peers"] if p.get("kind") == "owner"), cfg["peers"][0])
        state = load_json(STATE_FILE, {"cursors": {}, "pending": []})
        reply = handle(cfg, state, owner, [text])
        print(reply)
        return 0

    once = "--once" in sys.argv

    if os.path.exists(LOCK_FILE):
        try:
            proc_id = int(open(LOCK_FILE).read().strip())
            if proc_id != os.getpid():
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, proc_id)
                if h:
                    ctypes.windll.kernel32.CloseHandle(h)
                    print(f"已有实例在运行 (pid={proc_id})")
                    return 0
        except Exception:
            pass
    os.makedirs(TMP, exist_ok=True)
    open(LOCK_FILE, "w").write(str(os.getpid()))

    state = load_json(STATE_FILE, {"cursors": {}})
    state.setdefault("cursors", {})
    # 兼容旧状态
    if "lastLocalId" in state:
        owner = next((p for p in cfg["peers"] if p.get("kind") == "owner"), None)
        if owner:
            state["cursors"].setdefault(owner["wxid"], state["lastLocalId"])
        state.pop("lastLocalId", None)

    started = False
    log(f"桥接启动 pid={os.getpid()} peers={[p['name'] for p in cfg['peers']]}")
    db_path = os.path.join(DB_DIR, "message", "message_0.db")
    last_sig = None
    last_beat = 0
    try:
        while True:
            if os.path.exists(STOP_FILE):
                log("收到 stop 标记，退出")
                break
            if not os.path.exists(PAUSE_FILE):
                try:
                    st = os.stat(db_path)
                    wal = db_path + "-wal"
                    sig = (st.st_mtime_ns, st.st_size)
                    if os.path.exists(wal):
                        wt = os.stat(wal)
                        sig += (wt.st_mtime_ns, wt.st_size)
                    if sig != last_sig:
                        last_sig = sig
                        con = open_message_db(cfg)
                        names = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
                        pending = []
                        for peer in cfg["peers"]:
                            if peer["wxid"] not in state["cursors"]:
                                # 第一次见到这个联系人：从当前位置开始，不回旧消息
                                _, _, max_id = read_peer_rows(cfg, con, names, peer, 0)
                                state["cursors"][peer["wxid"]] = max_id
                                save_json(STATE_FILE, state)
                                continue
                            cursor = state["cursors"][peer["wxid"]]
                            msgs, max_id, _ = read_peer_rows(cfg, con, names, peer, cursor)
                            if msgs:
                                pending.append((peer, [t for _, t in msgs], max_id))
                            else:
                                state["cursors"][peer["wxid"]] = max(cursor, max_id)
                        con.close()
                        for peer, texts, max_id in pending:
                            if screen_locked():
                                if try_unlock():
                                    log("检测到锁屏，已自动解锁（无密码账户），继续处理")
                                else:
                                    log(f"屏幕锁着且解不开，{peer['name']} 的 {len(texts)} 条先压着")
                                    break
                            t0 = time.time()
                            log(f"处理 {peer['name']} {len(texts)} 条: {texts[0][:40]!r}")
                            try:
                                run_with_timeout(handle, cfg.get("handleTimeout", 300),
                                                 cfg, state, peer, texts)
                                state["cursors"][peer["wxid"]] = max_id
                                save_json(STATE_FILE, state)
                                log(f"处理完成 {peer['name']}，用时 {time.time() - t0:.1f}s")
                            except Exception as e:
                                log(f"处理 {peer['name']} 失败({time.time() - t0:.1f}s): {e}\n"
                                    + traceback.format_exc()[-800:])
                                if isinstance(e, TimeoutError):
                                    # 卡死的这条先跳过，别让整条桥接永久停摆
                                    state["cursors"][peer["wxid"]] = max_id
                                    save_json(STATE_FILE, state)
                                    log(f"已跳过 {peer['name']} 的这条，游标推进到 {max_id}")
                except Exception as e:
                    log(f"轮询出错: {e}")
            now = time.time()
            if now - last_beat >= 300:
                last_beat = now
                running = wechat_running()
                log(f"心跳 pid={os.getpid()} 微信={'在线' if running else '未运行'}"
                    f" 屏幕={'锁定' if screen_locked() else '解锁'}")
                # 每 5 分钟强制重扫一次：上一轮发送失败/睡眠打断的消息能自动重试
                last_sig = None
            if once:
                break
            time.sleep(cfg.get("pollSeconds", 2))
    finally:
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
