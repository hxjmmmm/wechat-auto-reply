# -*- coding: utf-8 -*-
"""
notify.py — Windows 桌面通知（Toast + 声音 + 日志兜底）

用法：
    python notify.py --title "联系人昵称" --message "在吗？"
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

# 输出统一成 UTF-8。不要用 io.TextIOWrapper 包一层 —— 两个坑：
#   1) pythonw / --noconsole 下 sys.stdout 是 None，摸 .buffer 会直接崩；
#   2) 被包住的原始流会在解释器退出时先关掉，包装器再 flush 就报
#      "I/O operation on closed file"。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _app_dir():
    """配置与数据的根目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录"""
    try:
        import runner
        return runner.app_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


HERE = _app_dir()
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
NOTIFY_LOG = os.path.join(DATA_DIR, "notifications.log")

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = $env:NOTIFY_TITLE
$m = $env:NOTIFY_BODY
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast duration='long'><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$n = New-Object Windows.UI.Notifications.ToastNotification $x
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($n)
"""


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def toast(title, message):
    """弹出 Windows 通知，返回是否成功"""
    env = dict(os.environ)
    env["NOTIFY_TITLE"] = _xml_escape(str(title)[:120])
    env["NOTIFY_BODY"] = _xml_escape(str(message)[:400])
    script = _PS.replace("$t = $env:NOTIFY_TITLE", f"$t = '{_xml_escape(str(title)[:120])}'")
    script = script.replace("$m = $env:NOTIFY_BODY", f"$m = '{_xml_escape(str(message)[:400])}'")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=25, env=env,
            # 无窗口父进程弹通知时，不给 powershell 新开一个黑框
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return r.returncode == 0
    except Exception:
        return False


def beep():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


def log_notification(title, message):
    try:
        with open(NOTIFY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title, "message": message,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def notify(title, message, sound=True):
    ok = toast(title, message)
    log_notification(title, message)
    if sound:
        beep()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="微信新消息")
    ap.add_argument("--message", required=True)
    ap.add_argument("--no-sound", action="store_true")
    args = ap.parse_args()
    ok = notify(args.title, args.message, sound=not args.no_sound)
    print(json.dumps({"toast_ok": ok}, ensure_ascii=False))


if __name__ == "__main__":
    main()
