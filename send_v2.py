# -*- coding: utf-8 -*-
"""
send_v2.py — 微信桌面端定向发送（带视觉校验锁）

为什么不用技能自带的 Alt+A：本机 Alt+A 被 Clash Verge 的全局热键抢占。
本脚本改用微信内置搜索框 Ctrl+F 导航，并在打字前用系统 OCR 校验
当前会话标题确实是目标联系人，校验失败立即 Esc 取消，绝不落字。

用法：
    python send_v2.py --contact "联系人昵称"                     # 只导航 + 校验（不发送）
    python send_v2.py --contact "联系人昵称" --message "在呢" --yes   # 校验通过后发送
"""
import argparse
import ctypes
import json
import os
import random
import subprocess
import sys
import time

# 输出统一成 UTF-8。不要用 io.TextIOWrapper 包一层 —— 两个坑：
#   1) --noconsole / pythonw 下 sys.stdout 是 None，摸 .buffer 会直接崩；
#   2) 被包住的原始流会在解释器退出时先关掉，包装器再 flush 就报
#      "I/O operation on closed file"。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import runner

import win32api
import win32con
import win32gui
import win32clipboard
import uiautomation as auto

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


def _asset(name):
    """定位伴随文件（脚本 / ps1）。

    打包后这些文件在 _MEIPASS 临时目录里，**不在 exe 旁边**，
    所以必须走 runner.script_path 找；只有都找不到时才回落到 exe 同级目录
    （用户自己放一份在那儿的情况）。
    """
    try:
        p = runner.script_path(name)
        if p:
            return p
    except Exception:
        pass
    return os.path.join(HERE, name)


SHOT_PS1 = _asset("_shot.ps1")
OCR_PS1 = _asset("ocr.ps1")

MAIN_CLASS = "Qt51514QWindowIcon"
MAIN_TITLE = "微信"


def log(msg):
    print(msg, flush=True)


def find_wechat():
    """微信最小化在托盘时 IsWindowVisible()==0，且 Qt 无边框没有 WS_CAPTION，
    所以这里不能按 visible / WS_CAPTION 过滤。"""
    res = []

    def cb(h, _):
        try:
            cls = win32gui.GetClassName(h)
            if cls != MAIN_CLASS:
                return
            title = win32gui.GetWindowText(h)
            if MAIN_TITLE not in title:
                return
            res.append(h)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return res


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def session_locked():
    """当前是否处于锁屏（已切到 Winlogon 安全桌面）状态。

    为什么要拦：锁屏后桌面切到安全桌面，SendInput / mouse_event 这类「硬件输入」
    会被路由到锁屏界面而不是微信窗口 —— 点击会落空、键盘输入可能落到别的窗口，
    **有把消息发错人的风险**。所以锁屏时宁可不发，让消息留在待回复队列，解锁后补发。

    判据（任一命中即视为锁屏，宁可保守）：
      1. 没有前台窗口 —— 锁屏时 GetForegroundWindow() 返回 0
      2. 拿不到鼠标位置 —— 锁屏时普通进程访问输入桌面会被拒绝（实测 Access denied）
      3. 打不开输入桌面

    ⚠️ 实测警告：这些判据**只在独立进程里准确**。本模块 import 了 uiautomation，
       导入之后同一套判据会失真（锁屏时依然返回 False），所以**不要拿它当发送闸门**，
       它是留给排查环境用的。真正可靠的闸门是「点击搜索框后检查前台窗口」那一道
       （见 main 里的 search 步骤）—— 锁屏时点击必然落空，窗口不会到前台，会直接中止。
    """
    try:
        u32 = ctypes.windll.user32
        if u32.GetForegroundWindow() == 0:
            return True
        pt = _POINT()
        if not u32.GetCursorPos(ctypes.byref(pt)):
            return True
        h = u32.OpenInputDesktop(0, False, 0x0001)
        if not h:
            return True
        u32.CloseDesktop(h)
        return False
    except Exception:
        return False


def activate(hwnd):
    if win32gui.IsIconic(hwnd):
        log("  [activate] 窗口最小化，还原中")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.2)
    if not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        time.sleep(0.8)
    try:
        w = auto.WindowControl(searchDepth=1, ClassName=MAIN_CLASS)
        if w.Exists(2):
            w.SetFocus()
    except Exception as e:
        log("  [activate] SetFocus 失败: %s" % e)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        log("  [activate] SetForegroundWindow 失败: %s" % e)
    time.sleep(0.9)
    return win32gui.GetForegroundWindow() == hwnd


def click_at(x, y):
    """点击指定绝对坐标；返回点击前的鼠标位置，便于事后还原。
    点击比 Ctrl+F 可靠：点击动作本身会激活窗口并把焦点给到输入框。"""
    try:
        old = win32api.GetCursorPos()
    except Exception:
        old = None          # 锁屏 / 受限环境下拿不到光标位置，不能因此崩掉
    try:
        win32api.SetCursorPos((int(x), int(y)))
        time.sleep(0.15)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.07)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as e:
        log("  [click] 点击失败: %s" % e)
    return old


def restore_mouse(old):
    """把鼠标放回原处。拿不到原位置（锁屏等）时静默跳过。"""
    if not old:
        return
    try:
        win32api.SetCursorPos(old)
    except Exception:
        pass


def set_clip(text):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def screenshot(path, rect=None):
    env = dict(os.environ)
    env["SHOT_PATH"] = path
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SHOT_PS1]
    if rect:
        x, y, w, h = rect
        cmd += ["-X", str(x), "-Y", str(y), "-W", str(w), "-H", str(h)]
    subprocess.run(cmd, env=env, capture_output=True, timeout=90,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return os.path.exists(path)


def ocr(path, out):
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    OCR_PS1, "-ImagePath", path, "-OutPath", out],
                   capture_output=True, timeout=180,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        txt = open(out, encoding="utf-8").read()
    except OSError:
        return False, ""
    ok = "OCR_OK" in txt
    body = txt.split("OCR_OK", 1)[1] if ok else txt
    return ok, body


def norm(s):
    return "".join(s.split())


def load_cfg():
    p = os.path.join(HERE, "config.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def verify_after_send(cfg, texts, minutes=5):
    """发送后读库校验：确认消息真的落进了她的会话表。

    这是截图 OCR 的替代方案 —— 不看屏幕，而且是精确匹配，比 OCR 模糊比对可靠。

    脚本和解释器都由 runner.cmd_for 决定：
      · 配置了 decrypt_python 且存在 → 用它跑verify_sent.py（源码模式）
      · 否则若当前是打包 exe      → 用 exe 自己重入（--script）
      · 否则用当前解释器 + 源码路径
    以前是按「exe 同级目录」找 verify_sent.py，打包后那儿根本没有，
    导致校验恒为失败 → 上层判成「发错会话」→ 消息被反复重发。
    """
    out = os.path.join(DATA_DIR, "sent_check.json")
    try:
        cmd = runner.cmd_for(cfg, "decrypt_python", "verify_sent.py")
    except Exception:
        cmd = []
    if not cmd:
        return {"ok": False, "error": "定位 verify_sent.py 失败（伴随脚本缺失）"}

    # 清掉上一次的旧结果 —— 否则校验没跑起来时会误读成"上次通过了"
    try:
        if os.path.exists(out):
            os.remove(out)
    except OSError:
        pass

    env = runner.child_env()
    cmd = cmd + ["--texts", "|".join(texts), "--minutes", str(minutes), "--out", out]
    try:
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", timeout=180, env=env,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with open(out, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def header_hit(contact, ocr_text):
    """OCR 质量有限，做分级判定：完整命中 > 连续2字命中 > 逐字命中比例"""
    t = norm(ocr_text)
    c = norm(contact)
    if not t or not c:
        return 0, "empty"
    if c in t:
        return 3, "full"
    for i in range(len(c) - 1):
        if c[i:i + 2] in t:
            return 2, "bigram:%s" % c[i:i + 2]
    hits = sum(1 for ch in set(c) if ch in t)
    if hits >= max(2, len(set(c)) // 2):
        return 1, "chars:%d/%d" % (hits, len(set(c)))
    return 0, "no-hit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", required=True)
    ap.add_argument("--message", default="")
    ap.add_argument("--message-file", default="")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--min-score", type=int, default=2, help="校验最低命中等级(1-3)")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过发送前的截图 OCR 校验（息屏时用它；改由发送后读库校验兜底）")
    ap.add_argument("--verify-after", action="store_true",
                    help="发送后读数据库确认消息确实落在目标会话（建议配合 --no-verify）")
    ap.add_argument("--split", action="store_true", help="按换行拆成多条依次发送")
    ap.add_argument("--gap-min", type=float, default=2.5, help="拆条最小间隔秒")
    ap.add_argument("--gap-max", type=float, default=6.0, help="拆条最大间隔秒")
    ap.add_argument("--delay-min", type=float, default=20.0, help="发送前最小等待秒")
    ap.add_argument("--delay-max", type=float, default=90.0, help="发送前最大等待秒")
    ap.add_argument("--no-delay", action="store_true", help="跳过随机延迟，立即发送")
    ap.add_argument("--search-x", type=int, default=170, help="搜索框相对窗口的 X 偏移")
    ap.add_argument("--search-y", type=int, default=50, help="搜索框相对窗口的 Y 偏移")
    ap.add_argument("--keep-mouse", action="store_true", help="不还原鼠标位置")
    args = ap.parse_args()

    if args.message_file:
        with open(args.message_file, encoding="utf-8") as f:
            args.message = f.read().strip()

    result = {"ok": False, "contact": args.contact, "stage": "start"}

    # 真人不会秒回：先随机等一段（放在导航前，避免等待期间会话被切走）
    if not args.no_delay and args.yes and args.message:
        wait = random.uniform(args.delay_min, args.delay_max)
        log("随机等待 %.0f 秒后开始发送" % wait)
        time.sleep(wait)

    hwnds = find_wechat()
    log("找到微信主窗口: %s" % [hex(h) for h in hwnds])
    if not hwnds:
        result["error"] = "未找到微信主窗口（请确认微信已登录）"
        print(json.dumps(result, ensure_ascii=False))
        return 1
    hwnd = hwnds[0]

    result["stage"] = "activate"
    fg = activate(hwnd)
    log("置前成功: %s" % fg)
    result["foreground"] = fg

    if not fg:
        # SetForegroundWindow 被系统拒绝是常态：Windows 只允许「当前前台进程」切换前台，
        # 后台脚本基本都会失败。**不能在这里中止** —— 下面那次真实鼠标点击本身就能
        # 把窗口带到前台（鼠标输入是系统认可的前台切换理由）。
        log("  （SetForegroundWindow 被系统拒绝，靠步2的真实点击来激活）")

    # 1) 点击微信内置搜索框（相对窗口偏移固定，与窗口大小无关）
    result["stage"] = "search"
    rect0 = win32gui.GetWindowRect(hwnd)
    old_mouse = click_at(rect0[0] + args.search_x, rect0[1] + args.search_y)
    time.sleep(0.9)

    # 关键安全闸：点击是一次真实鼠标输入，它本该把窗口带到前台。
    # 如果点完窗口还不在前台，说明这次操作根本没生效（典型：锁屏、窗口被遮挡），
    # 此时必须停 —— 否则接下来的打字会落到别的窗口，甚至发错会话。
    # 先给一次重试：个别情况下首次点击只是把窗口「预备激活」，第二次才真正生效。
    if win32gui.GetForegroundWindow() != hwnd:
        log("  点击后窗口未在前台，重试一次")
        click_at(rect0[0] + args.search_x, rect0[1] + args.search_y)
        time.sleep(1.0)
        rect0 = win32gui.GetWindowRect(hwnd)

    if win32gui.GetForegroundWindow() != hwnd:
        result["stage"] = "abort"
        result["error"] = ("点击后微信窗口仍未在前台，操作未生效（可能已锁屏）。"
                           "已取消，未发送任何内容；消息保留在待回复队列")
        log("✗ 点击后微信仍未在前台 → 中止，未发送任何内容")
        if not args.keep_mouse:
            try:
                restore_mouse(old_mouse)
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False))
        return 2

    auto.SendKeys("{Ctrl}a")
    time.sleep(0.25)
    set_clip(args.contact)
    auto.SendKeys("{Ctrl}v")
    time.sleep(1.8)
    log("已点击搜索框并粘贴联系人名，等待搜索结果")

    # 2) 回车进入会话
    result["stage"] = "enter"
    auto.SendKeys("{Enter}")
    time.sleep(1.3)

    # 3) 校验
    #    默认：截图 + OCR 比对窗口标题（需要屏幕亮着）
    #    --no-verify：跳过。搜索词在库里全局唯一，导航可信；改由发送后读库确认落点，
    #    那样更精确，而且不依赖屏幕 —— 息屏时走这条。
    if args.no_verify:
        result["stage"] = "verify-skipped"
        result["verify_score"] = None
        log("已跳过截图 OCR 校验（--no-verify），改由发送后读库确认")
    else:
        result["stage"] = "verify"
        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        w, h = x2 - x, y2 - y
        crop = (max(x, 0), max(y, 0), min(w, 900), min(110, h))
        shot = os.path.join(DATA_DIR, "verify.png")
        screenshot(shot, crop)
        screenshot(os.path.join(DATA_DIR, "verify_full.png"))
        ok, text = ocr(shot, os.path.join(DATA_DIR, "verify_ocr.txt"))
        log("OCR 可用=%s 窗口区域=%s" % (ok, crop))
        log("OCR 文本: %s" % text.replace("\n", " ")[:200])

        score, why = header_hit(args.contact, text)
        result["verify_score"] = score
        result["verify_why"] = why
        result["ocr_text"] = text.replace("\n", " ")[:300]
        log("校验结果: score=%s (%s)" % (score, why))

        if score < args.min_score:
            auto.SendKeys("{Esc}")
            time.sleep(0.3)
            auto.SendKeys("{Esc}")
            if not args.keep_mouse:
                restore_mouse(old_mouse)
            result["stage"] = "abort"
            result["error"] = "会话校验失败，已取消，未发送任何内容"
            print(json.dumps(result, ensure_ascii=False))
            return 2

    if not args.message or not args.yes:
        auto.SendKeys("{Esc}")
        if not args.keep_mouse:
            restore_mouse(old_mouse)
        result["stage"] = "verified-only"
        result["ok"] = True
        result["note"] = ("已跳过 OCR 校验（--no-verify）；未请求发送（缺 --message 或 --yes）"
                          if args.no_verify else "校验通过；未请求发送（缺 --message 或 --yes）")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # 4) 校验通过 → 发送（碎句型：拆成多条，间隔随机）
    result["stage"] = "send"
    parts = [p.strip() for p in args.message.split("\n") if p.strip()] if args.split else [args.message]
    result["parts"] = len(parts)
    for i, part in enumerate(parts):
        set_clip(part)
        auto.SendKeys("{Ctrl}v")
        time.sleep(0.5)
        auto.SendKeys("{Enter}")
        time.sleep(0.9)
        log("  已发送 %d/%d: %s" % (i + 1, len(parts), part))
        if i < len(parts) - 1:
            gap = random.uniform(args.gap_min, args.gap_max)
            time.sleep(gap)
    result["message"] = args.message
    if not args.keep_mouse:
        restore_mouse(old_mouse)

    # 5) 发送后读库校验：确认这几句话真的落在了她的会话表里
    if args.verify_after:
        time.sleep(1.5)  # 等消息落库
        cfg = load_cfg()
        chk = verify_after_send(cfg, parts)
        result["sent_check"] = chk
        log("发送后校验: ok=%s 命中=%s 未命中=%s %s"
            % (chk.get("ok"), chk.get("matched"), chk.get("missing"), chk.get("error") or ""))
        if chk.get("ok"):
            result["stage"] = "sent"
            result["ok"] = True
        elif chk.get("error"):
            # 校验脚本自己没跑起来 —— 这说明「没能确认」，不等于「发错了」。
            # 这两件事必须分开：以前混成 sent-wrong-session，上层就报
            # 「可能发到别的会话、队列保留」，下一轮再发一遍 ——
            # 实际结果是对方连着收到好几条一样的消息。
            result["stage"] = "sent-verify-error"
            result["ok"] = False
            result["error"] = "读库校验没跑起来：" + str(chk.get("error"))
        else:
            result["stage"] = "sent-wrong-session"
            result["ok"] = False
            result["error"] = "消息未出现在她的会话里 —— 可能导航到了别的会话，请立刻检查"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 3

    result["ok"] = True
    result["stage"] = "sent"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
