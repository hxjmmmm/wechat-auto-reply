# -*- coding: utf-8 -*-
"""
monitor.py — 检测目标联系人的新微信消息

原理：调用 wechat-cli 读取本机微信本地库中与目标联系人的聊天记录，
      用「消息行指纹」对比上次状态，从而识别出真正的新消息。

用法：
    python monitor.py --init          # 建立基线（把当前消息视为已读，不报警）
    python monitor.py                 # 检查新消息，输出 JSON
    python monitor.py --text          # 检查新消息，输出人类可读文本
    python monitor.py --context 12    # 同时输出最近 12 条上下文（供AI起草回复）
    python monitor.py --clear-pending # 确认发送成功后清空「待回复」队列

    # 阻塞等待模式 —— 省 token 的关键
    python monitor.py --wait --shift-minutes 52 --wait-timeout 280 --poll 20 \
                      --out data/check_result.json
      脚本自己每 20 秒查一次，**只有真的等到该回复的消息才返回**；
      期间模型完全不参与（一次调用 = 一次模型开销，而不是每 20 秒一次）。
      结束时 stdout 会打一行 ASCII 状态，便于程序化判断：
          WAKE new      fresh=N    ← 有她发来的新消息
          WAKE voice    ready=N    ← 语音转写结果好了（上一轮先跳过的那条）
          WAKE pending  pending=N  ← 上轮没发成功的遗留消息，重试
          NO_NEW        polls=N    ← 这一段没有需要回复的内容，继续等即可
          SHIFT_OVER               ← 本轮值守时间已满，可以收工了
          BUSY                     ← 已经有一个监听在跑，本次直接退出（不要重试）
          同时写到 data/wait_status.txt（纯 ASCII），供拿不到 stdout 时读

「待回复」队列（data/pending.json）：
    消息一旦被检测到就会标记为已见。若之后发送失败（息屏 / 校验拦截 / 进程中断），
    这条消息将永远不会被再次报出 —— 等于被吞掉。
    因此检测到的消息会同时进入 pending 队列，后续每轮都会重新报出，直到
    调用 --clear-pending 明确确认发送成功。
    若发现「她那条待回消息之后我已经发过消息」，会自动从队列移除（避免重复回）。

退出码：
    0  正常（--init / --clear-pending）
    1  出错（找不到会话 / wechat-cli 失败）
    10 有待回复消息（含上轮遗留）—— 等待模式下表示「被唤醒」
    11 无待回复消息 —— 等待模式下表示「等到超时也没有」
    12 值守时间已满（仅 --shift-minutes 生效时）
    13 已有另一个监听在运行（BUSY，仅等待模式）
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# 输出统一成 UTF-8。不要用 io.TextIOWrapper 包一层 —— 两个坑：
#   1) pythonw.exe（无控制台）下 sys.stdout 是 None，摸 .buffer 会直接崩，而且
#      因为没控制台连报错都看不见；
#   2) 被包住的原始流会在解释器退出时先关掉，包装器再 flush 就报
#      "I/O operation on closed file"。
# 用 reconfigure 两个坑都没有。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

import runner          # noqa: E402  统一「怎么调伴随脚本」（支持打包成 exe 后自包含运行）
import wxenv           # noqa: E402  读消息走它的 run_cli（外部 exe → 内置包自动回落）

# 配置与运行时数据的根目录。
# 打包成 exe 后 __file__ 指向临时解压目录，配置写那儿会每次丢失，
# 所以统一走 runner.app_dir()（= exe 所在目录）。
HERE = runner.app_dir()
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
INBOX_PATH = os.path.join(DATA_DIR, "inbox.jsonl")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")
PENDING_PATH = os.path.join(DATA_DIR, "pending.json")

MAX_SEEN = 400
# 语音转写是服务端异步生成的，刚收到语音时往往还没有。
# 最多重试这么多轮等转写出来，超过就按「听不到内容」处理，避免队列卡死。
VOICE_MAX_TRIES = 6


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    """原子写。

    临时文件名带 pid —— 以前固定叫 <path>.tmp，GUI（重建基线）和常驻监听
    同时写 state.json 时会互相把对方的临时文件删掉/覆盖，os.replace 直接抛错。
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def fp(line):
    """消息行指纹"""
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:16]


def msg_dt(line):
    """从消息行 '[2026-09-15 17:31] ...' 解析出 datetime"""
    try:
        return datetime.strptime(line[1:17], "%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        return None


def load_pending():
    return load_json(PENDING_PATH, {}).get("items") or []


def save_pending(items):
    save_json(PENDING_PATH, {"items": items,
                             "updated": datetime.now().isoformat(timespec="seconds")})


def clear_pending(fps=None):
    """清掉已确认回复成功的待回复消息。

    fps 为空 → 整个队列清空（重建基线等场景用，语义明确）。
    fps 为一组指纹 → **只移除这几条**。

    为什么要能按指纹删：一轮里可能同时有「能立刻回的文字」和「转写还没出来的语音」，
    后者本轮是跳过的、还留在队列里等下一轮。以前发送成功后整个文件被删掉，
    那条语音会一起消失 —— 而它的指纹早就进了 state.seen，等于永久丢失、再也不会被回复。
    """
    if not fps:
        try:
            if os.path.exists(PENDING_PATH):
                os.remove(PENDING_PATH)
        except OSError:
            pass
        return

    want = set(fps)
    keep = [it for it in load_pending() if it.get("fp") not in want]
    if keep:
        save_pending(keep)
    else:
        try:
            if os.path.exists(PENDING_PATH):
                os.remove(PENDING_PATH)
        except OSError:
            pass


def fetch_history(cfg):
    """拉取目标联系人最近消息，返回 (display_name, [line, ...])

    走 wxenv.run_cli：优先用 wechat-cli.exe，没有就回落到打包进来的 wechat_cli 包。
    """
    contact = (cfg.get("contact") or "").strip()
    if not contact:
        raise RuntimeError("还没配置要监听的联系人（config.json 的 contact）")
    limit = str(cfg.get("history_limit", 60))

    rc, out, err = wxenv.run_cli(cfg.get("wechat_cli"),
                                 ["history", contact, "--limit", limit, "--format", "json"])
    if rc != 0:
        # traceback 的关键信息（异常类型和消息）在末尾，取开头只会看到无用的调用栈框架
        msg = (err or out or "").strip()
        raise RuntimeError(f"读取聊天记录失败(rc={rc}): ...{msg[-700:]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"解析输出失败: {e}; 原始输出前200字: {out[:200]}")
    return data.get("chat") or contact, data.get("messages") or []


def decrypt_images(cfg, msg_times):
    """按消息时间解密对应的图片，返回 [{path, matched_at, ...}]

    解密脚本需要 pycryptodome，所以用 config 里的 decrypt_python（独立 venv）跑。
    失败不影响主流程 —— 返回空列表，由 agent 按"没看到图"处理。
    """
    script = runner.script_path("decrypt_image.py")
    base = runner.cmd_for(cfg, "decrypt_python", "decrypt_image.py")
    if not msg_times or not base or not script:
        return [], None

    out_path = os.path.join(DATA_DIR, "decrypted.json")
    cmd = base + ["--window", "300", "--out", out_path]
    for t in msg_times:
        cmd += ["--at", t]
    child_env = runner.child_env()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=180, env=child_env,
                           creationflags=runner.popen_flags())
        if r.returncode != 0 and not os.path.exists(out_path):
            return [], (r.stderr or r.stdout or "")[-400:]
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("images") or [], None
    except Exception as e:
        return [], repr(e)


def fetch_voice_texts(cfg, msg_times):
    """取语音消息的「转写文字」，返回 {消息时间: 文本}

    原理：微信的语音转写结果存在消息表的 packed_info_data 字段（protobuf），
    只是数据库是加密的，所以要走 voice_text.py 解密后再读。
    转写由服务端异步生成，刚收到时可能还没有 —— 拿不到就留空，下轮再试。
    """
    base = runner.cmd_for(cfg, "decrypt_python", "voice_text.py")
    if not msg_times or not base:
        return {}
    out_path = os.path.join(DATA_DIR, "voice_text.json")
    cmd = base + ["--out", out_path]
    for t in msg_times:
        cmd += ["--at", t]
    child_env = runner.child_env()
    try:
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                      errors="ignore", timeout=240, env=child_env,
                      creationflags=runner.popen_flags())
        if not os.path.exists(out_path):
            return {}
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        # 同一分钟可能有多条语音，所以按时间分组保留顺序，调用方按下标配对
        m = {}
        for v in (data.get("voices") or []):
            m.setdefault(v["time"], []).append(v["text"])
        return m
    except Exception:
        return {}


def is_from_her(line, display_name):
    """单聊中，来自对方的消息形如 '[时间] 对方昵称: 内容'，来自我的是 '[时间] me: 内容'"""
    after = line
    if line.startswith("[") and "] " in line:
        after = line.split("] ", 1)[1]
    return after.startswith(display_name + ": ")


def strip_prefix(line, display_name):
    """去掉 '[时间] 发送者: '，只留内容"""
    if "] " in line and line.startswith("["):
        after = line.split("] ", 1)[1]
        for who in (display_name + ": ", "me: "):
            if after.startswith(who):
                return after[len(who):]
        return after
    return line


def _dump_out(path, result):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"写入 --out 失败: {e}")


def write_status(text):
    """把等待状态写成纯 ASCII 文件，供拿不到 stdout 时读取"""
    try:
        with open(os.path.join(DATA_DIR, "wait_status.txt"), "w", encoding="ascii",
                  errors="replace") as f:
            f.write(text + "\n")
    except OSError:
        pass


def run_once(args):
    """执行一轮检测，返回 (退出码, 结果 dict 或 None)。quiet 时不打印。"""
    quiet = getattr(args, "_quiet", False)

    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    if args.clear_pending:
        clear_pending()
        log("PENDING cleared（已确认发送成功）")
        r = {"ok": True, "mode": "clear-pending"}
        say(json.dumps(r, ensure_ascii=False))
        return 0, r

    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"seen": [], "last_check": None})
    seen = set(state.get("seen") or [])

    try:
        display_name, messages = fetch_history(cfg)
    except Exception as e:
        log(f"ERROR {e}")
        say(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1, None

    # 新消息 = 指纹不在已见集合里
    new_lines = [m for m in messages if fp(m) not in seen]

    # 首次运行（还没有基线）：绝对不能把历史消息当成"新消息"回一遍。
    # 一个刚装好的工具，聊天记录里躺着几百条旧消息，如果直接开回就是灾难。
    # 所以第一轮只建基线 —— 把当前所有消息记为已见，之后才只看新增的。
    first_run = not args.init and not state.get("baseline_ready")
    if args.init or first_run:
        for m in messages:
            seen.add(fp(m))
        state["seen"] = list(seen)[-MAX_SEEN:]
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        state["baseline_ready"] = True
        save_json(STATE_PATH, state)
        clear_pending()
        mode = "init" if args.init else "baseline"
        log(f"{mode.upper()}: {len(messages)} 条消息已设为基线（不回复历史）；pending 已清空")
        r = {"ok": True, "mode": mode, "baseline_count": len(messages),
             "contact": display_name}
        say(json.dumps(r, ensure_ascii=False, indent=2))
        return 0, r

    # 「真新」= 本轮从聊天记录里新检测到的，区别于 pending 里上轮遗留的重复报出。
    # 等待模式靠它决定「要不要叫醒模型」：遗留消息不该触发唤醒，否则会空转烧 token。
    fresh_from_her = [m for m in new_lines if is_from_her(m, display_name)]
    new_from_her = list(fresh_from_her)
    new_from_me = [m for m in new_lines if not is_from_her(m, display_name)]

    # ---------- 待回复队列：保证「检测到了但没发出去」的消息不会丢 ----------
    # 背景：消息一旦被检测到就会标记为已见。如果后面发送失败（息屏、校验拦截、
    # 进程被打断），这条消息就再也不会被报出来 —— 等于被悄悄吞掉。
    # 所以把待回复的消息存进 pending，直到确认发送成功才由 --clear-pending 清掉。
    pending = load_pending()

    # 自动愈合：如果在她那条待回消息之后我已经发过消息，说明其实已经回过了。
    # 用「在 messages 列表里的先后位置」判断，比分钟级时间戳精确 —— 同一分钟内
    # 一前一后的两条消息，时间戳比不出先后。
    my_times = [t for t in (msg_dt(m) for m in messages
                            if not is_from_her(m, display_name)) if t]
    latest_my = max(my_times) if my_times else None
    idx_of = {}
    for i, m in enumerate(messages):
        idx_of[m] = i

    if pending:
        kept = []
        for it in pending:
            raw = it.get("raw", "")
            pos = idx_of.get(raw)
            if pos is not None:
                replied = any(not is_from_her(m, display_name) for m in messages[pos + 1:])
            else:
                # 已滚出读取窗口：退回用时间粗略判断
                t = msg_dt(raw)
                replied = bool(latest_my and t and latest_my >= t)
            if replied:
                log(f"PENDING 自动清除（其后已有我的消息）: {it.get('text', '')[:30]}")
            else:
                kept.append(it)
        pending = kept

    # 上一轮没发出去的，这轮补进来一起报
    reported_fps = {fp(m) for m in new_from_her}
    for it in pending:
        if not (it.get("fp") and it.get("raw")):
            continue
        if it["fp"] in reported_fps:
            continue
        # 语音还在等转写结果：重试几轮，超过上限就放弃（避免永远卡住队列）
        if it.get("voice_wait") and (it.get("tries") or 0) >= VOICE_MAX_TRIES:
            log(f"PENDING 放弃等待语音转写: {it.get('text', '')[:30]}")
            continue
        it["tries"] = (it.get("tries") or 0) + 1
        new_from_her.append(it["raw"])
        reported_fps.add(it["fp"])
    new_from_her.sort(key=lambda m: (msg_dt(m) or datetime.min))

    awaiting_reply = bool(messages) and is_from_her(messages[-1], display_name)

    # 更新状态
    for m in new_lines:
        seen.add(fp(m))
    state["seen"] = list(seen)[-MAX_SEEN:]
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    state["baseline_ready"] = True
    save_json(STATE_PATH, state)

    ctx = messages[-args.context:] if args.context > 0 else []

    result = {
        "ok": True,
        "contact": display_name,
        "checked_at": state["last_check"],
        "has_new": bool(new_from_her),
        "new_count": len(new_from_her),
        "fresh_count": len(fresh_from_her),
        "new_messages": [
            {"time": m.split("] ", 1)[0].lstrip("["), "text": strip_prefix(m, display_name), "raw": m}
            for m in new_from_her
        ],
        "my_new_count": len(new_from_me),
        "new_images": [],
        "pending_count": 0,
        "awaiting_reply": awaiting_reply,
        "last_message": strip_prefix(messages[-1], display_name) if messages else "",
        "last_sender_is_her": awaiting_reply,
        "context": [{"raw": m, "from_her": is_from_her(m, display_name),
                     "text": strip_prefix(m, display_name)} for m in ctx],
    }

    # ---------- 语音：直接读数据库里的转写文字 ----------
    # 语音的音频不落盘，但微信「语音转文字」的结果是落在消息表
    # packed_info_data 里的。读出来后，语音消息就变得跟文字消息一样可处理了。
    voice_times = [i["time"] for i in result["new_messages"] if "[语音]" in i["text"]]
    voice_wait = {}  # fp -> True，表示还在等转写
    if voice_times:
        vmap = fetch_voice_texts(cfg, voice_times)
        nth = {}  # 同一分钟里的第几条语音 —— 消息只精确到分钟，靠顺序配对
        for i in result["new_messages"]:
            if "[语音]" not in i["text"]:
                continue
            lst = vmap.get(i["time"]) or []
            k = nth.get(i["time"], 0)
            nth[i["time"]] = k + 1
            t = lst[k] if k < len(lst) else None
            if t:
                i["voice_text"] = t
                i["text"] = f"[语音] {t}"
            else:
                i["voice_text"] = None
                i["voice_pending"] = True
                voice_wait[fp(i["raw"])] = True
        log(f"语音转写: 请求 {len(voice_times)} 条 → 命中 {sum(1 for i in result['new_messages'] if i.get('voice_text'))} 条")

    # 写入待回复队列（保留上一轮未清掉的 + 本轮新检测到的，按指纹去重）
    now_iso = datetime.now().isoformat(timespec="seconds")
    pmap = {it["fp"]: it for it in pending if it.get("fp")}
    for m in new_from_her:
        f = fp(m)
        if f not in pmap:
            pmap[f] = {"fp": f, "raw": m, "time": m[1:17],
                       "text": strip_prefix(m, display_name), "detected_at": now_iso,
                       "tries": 1}
        # 语音没拿到转写的，下轮继续等；拿到了就标记一次，用于唤醒等待循环
        if voice_wait.get(f):
            pmap[f]["voice_wait"] = True
            pmap[f].pop("voice_ready", None)
        elif pmap[f].pop("voice_wait", None):
            pmap[f]["voice_ready"] = True
    # voice_ready 只上报一次就清掉，避免下一轮重复立即唤醒（空转烧 token）
    result["pending_voice_ready"] = sum(1 for it in pmap.values() if it.get("voice_ready"))
    for it in pmap.values():
        it.pop("voice_ready", None)
    save_pending(list(pmap.values()))
    result["pending_count"] = len(pmap)

    # 新消息里含图片 → 直接解密出图片文件，agent 用 Read 就能"看见"
    img_times = [i["time"] for i in result["new_messages"] if "[图片]" in i["text"]]
    if img_times:
        imgs, err = decrypt_images(cfg, img_times)
        result["new_images"] = imgs
        if err:
            result["decrypt_error"] = err
        log(f"图片解密: 请求 {len(img_times)} 张 → 成功 {len(imgs)} 张"
            + (f", 错误 {err[:120]}" if err else ""))

    # 记录到 inbox
    if new_from_her:
        try:
            with open(INBOX_PATH, "a", encoding="utf-8") as f:
                for item in result["new_messages"]:
                    f.write(json.dumps({"detected_at": state["last_check"], "contact": display_name,
                                        **item}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        log(f"NEW {len(new_from_her)} 条来自 {display_name}: "
            + " | ".join(i["text"][:40] for i in result["new_messages"]))

    # 等待模式（quiet）下不写 --out，由等待循环在唤醒那一刻才写，省一次无谓落盘
    if args.out and not quiet:
        _dump_out(args.out, result)

    if args.text and not args.json:
        if new_from_her:
            say(f"【{display_name}】有 {len(new_from_her)} 条新消息：")
            for i in result["new_messages"]:
                say(f"  [{i['time']}] {i['text']}")
        else:
            say(f"【{display_name}】无新消息。"
                + ("（最后一条是她发的，可能在等你回复）" if awaiting_reply else ""))
    else:
        say(json.dumps(result, ensure_ascii=False, indent=2))

    return (10 if new_from_her else 11), result


SHIFT_PATH = os.path.join(DATA_DIR, "shift.json")
LOCK_PATH = os.path.join(DATA_DIR, "watch.lock")
LOCK_STALE = 120     # 心跳超过这么久没更新，就认为那个监听已经死了
DAEMON_STALE_MIN = 180   # 常驻监听的心跳判活下限（见 heartbeat_alive）


def pid_alive(pid):
    """这个 pid 的进程还在跑吗"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def heartbeat_alive(path, min_stale=DAEMON_STALE_MIN):
    """按锁文件判断另一个监听实例是否还活着。

    优先看 pid 是否真的还在跑 —— 单轮最长能到十几分钟（读库 + 解密图片 + 读语音 +
    真实发送），而心跳只在每轮开头写一次。用固定阈值判断就会误判成「已死」：
    界面显示未运行、还能再起一个监听，结果同一个会话被两个进程同时盯、每条消息回两遍。

    只有拿不到进程信息（比如跨平台 / 权限受限）时才退回心跳时间，
    且那时的阈值按轮询间隔放宽，不再写死 120 秒。
    """
    d = load_json(path, {}) or {}
    if pid_alive(d.get("pid")):
        return True
    hb = d.get("heartbeat_ts")
    if not hb:
        return False
    stale = max(min_stale, 3 * int(d.get("interval") or 0))
    return (time.time() - hb) < stale


def lock_alive():
    """是否已有另一个「阻塞等待」在跑（防止定时任务叠加、重复回复）"""
    return heartbeat_alive(LOCK_PATH, LOCK_STALE)


def lock_touch(interval=0):
    save_json(LOCK_PATH, {"pid": os.getpid(), "interval": int(interval or 0),
                          "heartbeat_ts": time.time(),
                          "heartbeat": datetime.now().isoformat(timespec="seconds")})


def lock_release():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def shift_remaining(args):
    """值守窗口管理：返回本次还能等多少秒；返回 None 表示本次值守该收工了。

    为什么需要：自动化每小时才被触发一次，触发后要连续盯 50 分钟左右。
    由脚本记住「本次值守从什么时候开始」，调用方就不必自己数轮数、算时间。
    值守结束后把记录删掉，下一个小时重新开始一轮。
    """
    if args.shift_minutes <= 0:
        return args.wait_timeout
    sh = load_json(SHIFT_PATH, {})
    started = sh.get("started_at")
    now = time.time()
    # 没有记录，或记录已经过期很久（进程被中断留下的残留）→ 开一轮新的
    if not started or (now - started) > args.shift_minutes * 60 + 180:
        started = now
        save_json(SHIFT_PATH, {"started_at": started,
                               "started": datetime.now().isoformat(timespec="seconds")})
    remain = args.shift_minutes * 60 - (now - started)
    if remain < 30:
        return None
    return int(min(args.wait_timeout, remain))


def wait_loop(args):
    """阻塞等待：脚本自己轮询，只在「真的该回复了」的时候才返回。

    为什么要有这个：以前是模型每 45 秒跑一次 monitor + 读一次结果，
    一次空闲小时要几十次模型调用，token 全烧在「看有没有消息」上。
    现在检测循环完全在脚本里跑，模型只在被唤醒时才参与一次。
    """
    # 已经有一个监听在跑 → 直接退出（防定时任务叠加：多个监听同时盯同一个会话，
    # 既烧 token，又可能在发送时互相打架）
    if lock_alive():
        print("BUSY 已有一个监听在运行，本次直接结束（不要重试）")
        write_status("BUSY")
        log("WAIT 拒绝启动：已有监听在跑")
        return 13, None

    window = shift_remaining(args)
    if window is None:
        try:
            os.remove(SHIFT_PATH)
        except OSError:
            pass
        print(f"SHIFT_OVER 值守已满 {args.shift_minutes} 分钟，本次收工")
        write_status(f"SHIFT_OVER minutes={args.shift_minutes}")
        log("WAIT 值守结束")
        return 12, None

    lock_touch()
    start = time.monotonic()
    deadline = start + window
    last_wake = 0.0          # 上一次「遗留消息重试」唤醒的时间
    polls = 0
    try:
        while True:
            polls += 1
            code, result = run_once(args)
            if result is None:
                print(f"ERROR rc={code} 检测失败（详见 data/monitor.log）")
                write_status(f"ERROR code={code} polls={polls}")
                return 1, None

            fresh = int(result.get("fresh_count") or 0)
            pend = int(result.get("pending_count") or 0)
            voice_ready = int(result.get("pending_voice_ready") or 0)
            now = time.monotonic()

            if fresh > 0:
                reason, detail = "new", f"fresh={fresh} pending={pend}"
            elif voice_ready > 0:
                reason, detail = "voice", f"ready={voice_ready}"
            elif pend > 0 and (now - last_wake) >= args.pending_retry:
                reason, detail = "pending", f"pending={pend}"
            else:
                reason = ""

            elapsed = int(now - start)
            if reason:
                last_wake = now
                if args.out:
                    _dump_out(args.out, result)
                print(f"WAKE {reason} {detail} polls={polls}")
                write_status(f"WAKE {reason} {detail} polls={polls}")
                log(f"WAIT 唤醒[{reason}] {detail} 轮数={polls} 耗时={elapsed}s")
                return 10, result

            if now >= deadline:
                if args.out:
                    _dump_out(args.out, result)
                print(f"NO_NEW polls={polls} waited={elapsed}s")
                write_status(f"NO_NEW polls={polls} waited={elapsed}s")
                log(f"WAIT 超时无消息 轮数={polls} 耗时={elapsed}s")
                return 11, result

            lock_touch(args.poll)      # 心跳：告诉后来的监听「我还活着」
            time.sleep(max(1.0, min(args.poll, deadline - now)))
    finally:
        lock_release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="建立基线，不报警")
    ap.add_argument("--text", action="store_true", help="文本输出")
    ap.add_argument("--context", type=int, default=10, help="输出最近 N 条上下文")
    ap.add_argument("--json", action="store_true", help="强制 JSON 输出")
    ap.add_argument("--out", default="", help="把结果额外写入指定 UTF-8 文件（推荐，避免管道编码问题）")
    ap.add_argument("--clear-pending", action="store_true",
                    help="清空「待回复」队列（发送成功并确认后调用）")
    ap.add_argument("--wait", action="store_true",
                    help="阻塞等待模式：脚本自己轮询，等到该回复的消息才返回（省 token）")
    ap.add_argument("--wait-timeout", type=int, default=420,
                    help="等待模式的最长等待秒数（默认 420；请让工具超时比它大 60s 以上）")
    ap.add_argument("--poll", type=int, default=30, help="等待模式的轮询间隔秒数（默认 30）")
    ap.add_argument("--pending-retry", type=int, default=300,
                    help="上轮没发成功的遗留消息，至少隔这么多秒才再唤醒一次（默认 300）")
    ap.add_argument("--shift-minutes", type=int, default=0,
                    help="值守窗口：脚本自己记住本轮值守起点，满这么多分钟就返回 SHIFT_OVER(12)；"
                         "0 = 不启用（每次调用独立计时）")
    args = ap.parse_args()

    if args.wait:
        args._quiet = True
        if args.init:
            code, _ = run_once(args)
            return code
        return wait_loop(args)[0]

    return run_once(args)[0]


if __name__ == "__main__":
    sys.exit(main())
