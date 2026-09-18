# -*- coding: utf-8 -*-
"""
watch.py — 实时监听目标联系人的新消息（常驻轮询）

行为：
  * 每 poll_seconds 秒读取一次微信本地库中与该联系人的最新消息
  * 一旦发现新消息来自她 → 弹 Windows 通知 + 记入 inbox.jsonl
  * 若 config.json 中 auto_send=true 且有 auto_reply_regex 匹配 → 自动回复（谨慎开启）
  * 解密失败会自动尝试重新提取密钥（用 config 里的 db_dir）

用法：
    python watch.py                 # 常驻运行（Ctrl+C 停止）
    python watch.py --once          # 只跑一轮（便于测试）
    python watch.py --minutes 60    # 运行 60 分钟后自动退出
    python watch.py --quiet         # 不弹通知，只写日志
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor  # noqa: E402
import notify   # noqa: E402

# 输出统一成 UTF-8（不用 io.TextIOWrapper 包一层：无控制台时 sys.stdout 为 None 会崩）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def reinit_keys(cfg):
    """微信重启后密钥可能失效，尝试重新提取"""
    exe = cfg.get("wechat_cli")
    db_dir = cfg.get("db_dir")
    if not exe or not db_dir:
        return False
    try:
        args = [exe, "init", "--force"]
        args += ["--db-dir", db_dir]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=600)
        ok = r.returncode == 0
        log(f"REINIT {'成功' if ok else '失败'}: {(r.stdout or '')[-200:]}")
        return ok
    except Exception as e:
        log(f"REINIT 异常: {e}")
        return False


def detect(cfg):
    """返回 (display_name, new_from_her[list of dict], awaiting_reply, all_lines)"""
    state = monitor.load_json(STATE_PATH, {"seen": [], "baseline_ready": False})
    seen = set(state.get("seen") or [])

    display_name, messages = monitor.fetch_history(cfg)

    if not state.get("baseline_ready"):
        for m in messages:
            seen.add(monitor.fp(m))
        state["seen"] = list(seen)[-monitor.MAX_SEEN:]
        state["baseline_ready"] = True
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        monitor.save_json(STATE_PATH, state)
        log(f"首个基线已建立（{len(messages)} 条），之后只提醒新消息")
        return display_name, [], False, messages

    new_lines = [m for m in messages if monitor.fp(m) not in seen]
    new_from_her = [m for m in new_lines if monitor.is_from_her(m, display_name)]

    for m in new_lines:
        seen.add(monitor.fp(m))
    state["seen"] = list(seen)[-monitor.MAX_SEEN:]
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    monitor.save_json(STATE_PATH, state)

    awaiting = bool(messages) and monitor.is_from_her(messages[-1], display_name)

    items = []
    for m in new_from_her:
        items.append({
            "time": m.split("] ", 1)[0].lstrip("["),
            "text": monitor.strip_prefix(m, display_name),
            "raw": m,
        })
    if items:
        try:
            with open(os.path.join(DATA_DIR, "inbox.jsonl"), "a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps({"detected_at": state["last_check"],
                                        "contact": display_name, **it},
                                       ensure_ascii=False) + "\n")
        except OSError:
            pass
    return display_name, items, awaiting, messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = monitor.load_json(CONFIG_PATH, {})
    interval = max(5, int(cfg.get("poll_seconds", 20)))
    contact = cfg.get("contact", "")

    log(f"===== 开始监听「{contact}」，间隔 {interval}s =====")
    if not args.quiet:
        notify.notify(f"微信监听已启动", f"正在关注「{contact}」的新消息", sound=False)

    started = time.time()
    deadline = started + args.minutes * 60 if args.minutes > 0 else None
    fails = 0

    while True:
        try:
            name, items, awaiting, messages = detect(cfg)
            fails = 0
            if items:
                head = items[0]["text"][:80]
                more = f"（共 {len(items)} 条）" if len(items) > 1 else ""
                log(f"NEW {len(items)} 条来自 {name}: {head}{more}")
                if not args.quiet:
                    notify.notify(f"💬 {name} 发来消息{more}", head)
            else:
                log(f"无新消息（最后一条{'是她发的' if awaiting else '是我发的'}）")
        except Exception as e:
            fails += 1
            log(f"ERROR 第{fails}次: {e}")
            if fails in (3, 10, 30):
                log("尝试重新提取微信密钥…")
                reinit_keys(cfg)
            time.sleep(min(90, interval * min(fails, 5)))

        if args.once:
            break
        if deadline and time.time() > deadline:
            log("到达运行时限，退出")
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
