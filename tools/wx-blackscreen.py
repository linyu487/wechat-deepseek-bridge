"""插电时的纯黑遮罩：屏幕保持常亮但显示纯黑，避免熄屏/锁屏打断桥接。

规则：
- 只在插电时生效；拔电立刻清掉保持唤醒，让系统照常休眠。
- 空闲 3 分钟自动盖黑；鼠标/键盘一动就隐藏。
- 桥接正在发消息时（tmp/blackscreen.sending 存在）不因为模拟输入而隐藏。
- 全屏游戏/演示时不盖黑。
- 进程靠 tmp/blackscreen.lock 单例，日志 tmp/blackscreen.log。
"""

import ctypes
import os
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, "tmp")
LOCK = os.path.join(TMP, "blackscreen.lock")
MARKER = os.path.join(TMP, "blackscreen.sending")
LOG = os.path.join(TMP, "blackscreen.log")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
IDLE_SHOW = 180


def log(msg):
    try:
        os.makedirs(TMP, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def pid_is_python(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return "python" in buf.value.lower()
    finally:
        k32.CloseHandle(h)
    return False


def ac_online():
    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]
    s = SPS()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        return bool(ok and s.ACLineStatus == 1)
    except Exception:
        return False


def idle_seconds():
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    l = LII()
    l.cbSize = ctypes.sizeof(l)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(l)):
        return (ctypes.windll.kernel32.GetTickCount() - l.dwTime) / 1000.0
    return 0.0


def fullscreen_app():
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value in (3, 4)
    except Exception:
        pass
    return False


def sending_now():
    try:
        if not os.path.exists(MARKER):
            return False
        return time.time() - os.path.getmtime(MARKER) < 120
    except Exception:
        return False


def make_window():
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="none")
    w = root.winfo_screenwidth()
    h = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+0+0")
    hwnd = root.winfo_id()
    try:
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass
    root.withdraw()
    return root


def main():
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK, encoding="utf-8").read().strip())
            if old != os.getpid() and pid_is_python(old):
                return 0
        except Exception:
            pass
    os.makedirs(TMP, exist_ok=True)
    open(LOCK, "w", encoding="utf-8").write(str(os.getpid()))
    log(f"blackscreen 启动 pid={os.getpid()}")
    root = make_window()
    shown = [False]

    def tick():
        try:
            if not ac_online():
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                if shown[0]:
                    root.withdraw()
                    shown[0] = False
                return
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            idle = idle_seconds()
            busy = sending_now() or fullscreen_app()
            if not shown[0] and idle >= IDLE_SHOW and not busy:
                root.deiconify()
                root.attributes("-topmost", True)
                root.lift()
                shown[0] = True
                log("盖黑")
            elif shown[0] and idle < 2 and not busy:
                root.withdraw()
                shown[0] = False
                log("用户回来，取消盖黑")
        except Exception as e:
            log(f"tick 出错: {e}")
        finally:
            root.after(1000, tick)

    root.after(1000, tick)
    try:
        root.mainloop()
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        try:
            os.remove(LOCK)
        except Exception:
            pass
        log("blackscreen 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
