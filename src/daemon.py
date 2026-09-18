# -*- coding: utf-8 -*-
"""daemon.py — 常驻监听目标联系人并自动回复（平时 0 token）

和以前的方案差在哪：

    旧：WorkBuddy 定时任务每小时叫醒模型 → 模型反复调 monitor.py --wait 去等消息
        → 空闲一小时也要烧掉 11～12 次模型调用。
    新：本脚本常驻在后台，用纯 Python 每 20 秒查一次本地库（**不花任何 token**），
        **只有她真的发了消息**，才用一次极小的 HTTP 请求（几百 token）生成回复。

也就是说：被叫醒的次数 = 她实际发消息的次数，中间的空档一次都不花。

它做的事：
    1. 复用 monitor.py 的检测逻辑（新消息 / 图片解密 / 语音转写 / 待回复队列）
    2. 轮询期间持续心跳 monitor 的 watch.lock → 万一老定时任务还在跑，它会拿到 BUSY 直接退出，不会重复回复
    3. 有消息 → reply_engine 用一次 HTTP 请求生成回复 → 写 data/reply.txt
    4. 调 send_v2.py 发送（真实键鼠，需解锁桌面）→ 读库校验落点 → 成功才清待回复队列
    5. 发送失败（锁屏等）→ 消息留在待回复队列，过一会儿自动重试
    6. 任何异常都不会让进程退出（看门狗循环），只会记日志后继续

用法：
    python daemon.py                 # 常驻运行（建议用 pythonw.exe 起，无窗口）
    python daemon.py --once          # 只跑一轮（测试用）
    python daemon.py --once --no-send   # 只检测 + 生成回复，不发送
    python daemon.py --test-llm      # 用一句假消息试一次模型，验证 key 配好没有
    python daemon.py --status        # 看运行状态、统计
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# 用 pythonw.exe 起时没有控制台，sys.stdout / sys.stderr 会是 None。
# 而 monitor.py 在 import 那一刻就会去摸 sys.stdout.buffer —— 必须先垫上，
# 否则整个进程一 import 就崩，而且因为没控制台，连报错都看不见。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

import runner           # noqa: E402
import monitor          # noqa: E402
import reply_engine     # noqa: E402

# 打包成 exe 后 __file__ 是临时解压目录，配置/数据必须落在 exe 同级目录
HERE = runner.app_dir()
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(HERE, "config.json")
DAEMON_LOG = os.path.join(DATA_DIR, "daemon.log")
DAEMON_LOCK = os.path.join(DATA_DIR, "daemon.lock")

# 全局实例锁 —— 放在用户目录，**所有**实例都能看到。
#
# 为什么需要它：exe 版的数据目录是 dist\data，源码版是项目根的 data，
# 两边各写各的 daemon.lock，互相根本看不见。结果就是能同时开两个监听，
# 同一个会话被两个进程一起盯：每条消息回两遍，而且两个进程抢同一个微信窗口，
# 发送被拖到八九十秒、多条内容还会粘成一条发过去。
# 这把锁路径固定，谁先起来谁占住，后来的直接退出。
GLOBAL_LOCK_DIR = os.path.join(os.environ.get("LOCALAPPDATA")
                               or os.path.expanduser("~"), "wechat-auto-reply")
GLOBAL_LOCK = os.path.join(GLOBAL_LOCK_DIR, "instance.lock")
REPLY_PATH = os.path.join(DATA_DIR, "reply.txt")
SEND_RESULT = os.path.join(DATA_DIR, "send_result.json")
STATS_PATH = os.path.join(DATA_DIR, "daemon_stats.json")
STOP_FLAG = os.path.join(DATA_DIR, "stop.flag")


def stop_requested():
    return os.path.exists(STOP_FLAG)


def request_stop(by="unknown"):
    """请求常驻进程停止（图形界面「停止」按钮 / --stop 调它）"""
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} by={by}\n")
        return True
    except OSError:
        return False


def clear_stop():
    """进程启动时先清掉上次遗留的停止标记，否则会一起来就退出"""
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass


def auto_send_enabled(cfg):
    """是否自动发送。关掉＝只生成草稿（data/reply.txt）并通知，不碰微信。"""
    return bool(cfg.get("auto_send", True))


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(DAEMON_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 单实例锁

def self_lock_alive():
    """常驻进程是否还在跑。

    走 monitor.heartbeat_alive：优先用 pid 探活，不再靠固定 120 秒心跳
    —— 单轮最长十几分钟，固定阈值会误判，导致能再起一个监听、重复回复。
    """
    return monitor.heartbeat_alive(DAEMON_LOCK)


def _lock_payload(interval=0):
    return {"pid": os.getpid(), "interval": int(interval or 0),
            "heartbeat_ts": time.time(),
            "heartbeat": datetime.now().isoformat(timespec="seconds"),
            "source": "exe" if runner.is_frozen() else "源码",
            "app_dir": HERE}


def global_lock_alive():
    """有没有**别的**实例在跑（exe 版 / 源码版都算）。返回它的信息，没有则返回 None"""
    try:
        os.makedirs(GLOBAL_LOCK_DIR, exist_ok=True)
    except OSError:
        pass
    if not os.path.exists(GLOBAL_LOCK):
        return None
    info = monitor.load_json(GLOBAL_LOCK, {}) or {}
    if not info or info.get("pid") == os.getpid():
        return None          # 锁是自己的，不算冲突
    # 只认 pid 探活，不看心跳时间：进程没了就是没了。
    # 否则「被强杀留下的锁」会因为心跳还算新鲜而被当成活实例，
    # 结果是被杀掉的那个还能挡住新实例两分钟。
    try:
        return info if monitor.pid_alive(info.get("pid")) else None
    except Exception:
        return info if monitor.heartbeat_alive(GLOBAL_LOCK) else None


def self_lock_touch(interval=0):
    payload = _lock_payload(interval)
    monitor.save_json(DAEMON_LOCK, payload)
    # 全局锁一起更新，让另一个目录里的实例也能看到我
    try:
        monitor.save_json(GLOBAL_LOCK, payload)
    except OSError:
        pass


def self_lock_release():
    for p in (DAEMON_LOCK, GLOBAL_LOCK):
        try:
            # 全局锁只在「确实是自己的」时候删，别把别人的锁删了
            if p == GLOBAL_LOCK:
                info = monitor.load_json(p, {}) or {}
                if info.get("pid") not in (None, os.getpid()):
                    continue
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- 统计

def stats_bump(**kw):
    s = monitor.load_json(STATS_PATH, {})
    for k, v in kw.items():
        s[k] = (s.get(k) or 0) + v
    s["updated"] = datetime.now().isoformat(timespec="seconds")
    monitor.save_json(STATS_PATH, s)
    return s


# ---------------------------------------------------------------- 检测

class _Ns:
    pass


def _monitor_args(context):
    a = _Ns()
    a.clear_pending = False
    a.init = False
    a.context = context
    a.out = ""
    a.json = False
    a.text = False
    a._quiet = True          # 不打印，daemon 自己记日志
    return a


def run_monitor(context):
    return monitor.run_once(_monitor_args(context))


def reinit_keys(cfg):
    """微信重启后本地库密钥可能失效，尝试重新提取"""
    exe = cfg.get("wechat_cli")
    db_dir = cfg.get("db_dir")
    if not exe or not db_dir:
        return False
    try:
        r = subprocess.run([exe, "init", "--force", "--db-dir", db_dir],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=600,
                           creationflags=runner.popen_flags())
        log(f"重新提取密钥 {'成功' if r.returncode == 0 else '失败'}: "
            + ((r.stdout or r.stderr or "")[-200:]))
        return r.returncode == 0
    except Exception as e:
        log(f"重新提取密钥异常: {e}")
        return False


def prep_result(result):
    """去掉"语音转写还没出来"的消息（这轮先不回，等 WAKE voice 再叫）。

    返回 (可用消息列表, 被跳过的条数)
    """
    usable, skipped = [], 0
    for m in result.get("new_messages") or []:
        if m.get("voice_pending"):
            skipped += 1
            continue
        usable.append(m)
    return usable, skipped


# ---------------------------------------------------------------- 生成 + 发送

def write_reply(lines):
    with open(REPLY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def send_reply(cfg):
    """调 send_v2.py 真实发送，返回 (ok, stage, detail)"""
    base = runner.cmd_for(cfg, "send_python", "send_v2.py")
    script = runner.script_path("send_v2.py")
    if not base or not script:
        return False, "no-sender", f"发送环境缺失: base={base} script={script}"

    cmd = base + [
        # 默认值留空：以前写死成一个具体昵称，别人拿到 exe 且没配联系人时
        # 会直接拿那个名字去搜索，既跑不通也等于把私人信息打了出去
        "--contact", cfg.get("contact", ""),
        "--message-file", REPLY_PATH,
        "--split", "--yes", "--no-verify", "--verify-after"]
    child_env = runner.child_env()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=420, cwd=HERE, env=child_env,
                           creationflags=runner.popen_flags())
    except subprocess.TimeoutExpired:
        return False, "timeout", "发送超时"
    except Exception as e:
        return False, "exception", repr(e)

    # 只认「最后一行以 { 开头」的 JSON —— send_v2 前面会打一堆日志，
    # 从第一个 { 开始切的话，日志里只要出现 { 就会解析错。
    raw = (r.stdout or "").strip()
    res = {}
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                res = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                res = {}
            break
    try:
        with open(SEND_RESULT, "w", encoding="utf-8") as f:
            json.dump({"rc": r.returncode, "result": res, "stderr": (r.stderr or "")[-800:]},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    stage = res.get("stage") or f"rc={r.returncode}"
    chk = res.get("sent_check") or {}
    if r.returncode == 0 and stage == "sent":
        missing = chk.get("missing") or []
        if not missing:
            return True, stage, "已确认落在她的会话"
        return False, "sent-partial", f"部分没落地: {missing}"
    if stage == "abort":
        return False, stage, "微信窗口没到前台（多半是锁屏/息屏），一个字都没发"
    if stage == "sent-wrong-session":
        return False, stage, "⚠️ 可能发到别的会话去了，请人工检查"
    if stage == "sent-verify-error":
        # 消息已经敲进去并回车了，只是读库复核那一步没跑起来。
        # 跟「发错会话」是两回事，别用吓人的措辞，也别让上层保留队列。
        return False, stage, "已发送，但读库复核没跑起来：" + str(chk.get("error") or "未知原因")
    return False, stage, f"rc={r.returncode} {(res.get('error') or '')}".strip()


def _notify(cfg, title, message):
    """桌面通知。

    config.notify 为 false 时一个都不弹 —— 以前这个开关是死的：
    界面里认真存进 config.json，daemon 却从来不读，关掉了照样弹。
    """
    if not cfg.get("notify", True):
        return False
    try:
        import notify
        return bool(notify.notify(title, (message or "")[:200]))
    except Exception:
        return False


def handle_new(cfg, result, args):
    """有该回复的消息 → 生成回复并发送。返回是否发送成功。"""
    usable, skipped = prep_result(result)
    if not usable:
        if skipped:
            log(f"跳过 {skipped} 条消息（语音转写还没生成，等下一轮唤醒）")
        return False

    result = dict(result, new_messages=usable)
    # 本轮真正被回复掉的指纹 —— 发送成功后只清这些，
    # 队列里还没处理的（比如转写没出来的语音）必须留着，不能在一次成功里被顺手清掉。
    replied_fps = [monitor.fp(m["raw"]) for m in usable if m.get("raw")]
    preview = " | ".join((m.get("text") or "")[:30] for m in usable)
    log(f"准备回复：{preview}")

    try:
        lines, meta = reply_engine.generate(cfg, result, log=log)
    except Exception as e:
        log(f"生成失败: {e}")
        stats_bump(llm_errors=1)
        return False

    if not lines:
        log("模型没给出可用回复，跳过")
        return False

    log("回复草稿：" + " / ".join(lines))
    write_reply(lines)
    stats_bump(llm_calls=1, replies=1)

    if args.no_send or not auto_send_enabled(cfg):
        why = "--no-send" if args.no_send else "auto_send=false（草稿模式）"
        log(f"{why}：草稿已写入 data/reply.txt，未发送")
        _notify(cfg, "微信自动回复 · 草稿", " / ".join(lines))
        # 草稿模式视为已处理，清掉队列 —— 否则每 300 秒会重复生成一遍、把草稿覆盖掉
        monitor.clear_pending(replied_fps)
        stats_bump(drafts=1)
        return False

    ok, stage, detail = send_reply(cfg)
    if ok:
        log(f"发送成功（{stage}）：{detail}")
        monitor.clear_pending(replied_fps)
        stats_bump(sent=1)
        _notify(cfg, "微信自动回复 · 已发送", " / ".join(lines))
        return True

    log(f"发送未成功（{stage}）：{detail}")
    stats_bump(send_failed=1)
    if stage == "sent-wrong-session":
        log("⚠️ 注意：消息可能发到了别的会话，待回复队列已保留，请人工确认后再处理")
    elif stage == "sent-verify-error":
        # 消息已经发出去了，只是没能用读库复核。这时候如果保留队列，
        # 下一轮会原样再发一遍 —— 对方收到的是一串重复消息（实测连发三条）。
        # 所以这里清队列，只留一句让人确认一次的提示。
        log("ℹ️ 消息已发出，只是读库复核没跑起来；队列已清除以免重复发送。"
            "请打开微信确认一次，并看 data/sent_check.json 里的 error")
        monitor.clear_pending(replied_fps)
        stats_bump(sent=1)
    return False


# ---------------------------------------------------------------- 主循环

def loop_once(cfg, args, state):
    """跑一轮检测；有需要就生成并发送。返回唤醒原因（无则空字符串）"""
    _iv = state.get("interval", 0)
    monitor.lock_touch(_iv)  # 心跳：让老的 --wait 监听（若有）拿到 BUSY
    self_lock_touch(_iv)

    code, result = run_monitor(args.context)
    if result is None:
        state["fails"] += 1
        log(f"检测失败 rc={code}（第 {state['fails']} 次，详见 data/monitor.log）")
        if state["fails"] in (3, 10, 30):
            log("连续失败，尝试重新提取微信密钥…")
            reinit_keys(cfg)
        return ""

    state["fails"] = 0

    # 首次运行只建基线，不回历史消息（否则刚装好就会把几百条旧消息全回一遍）
    if result.get("mode") in ("init", "baseline"):
        log(f"首次运行：已把当前 {result.get('baseline_count', 0)} 条聊天记录设为基线，"
            f"从下一条新消息开始回复")
        return ""

    fresh = int(result.get("fresh_count") or 0)
    voice_ready = int(result.get("pending_voice_ready") or 0)
    pend = int(result.get("pending_count") or 0)
    now = time.time()

    if fresh > 0:
        reason = f"new fresh={fresh}"
    elif voice_ready > 0:
        reason = f"voice ready={voice_ready}"
    elif pend > 0 and (now - state["last_pending_wake"]) >= args.pending_retry:
        reason = f"pending pending={pend}"
    else:
        return ""

    state["last_pending_wake"] = now
    log(f"检测到待回复内容 → {reason}")
    handle_new(cfg, result, args)
    return reason


def supervise(cfg, args):
    state = {"fails": 0, "last_pending_wake": 0.0,
             "interval": max(5, int(args.interval or cfg.get("poll_seconds", 20)))}
    interval = state["interval"]
    clear_stop()          # 清掉上次遗留的停止标记，否则一起来就退出
    log(f"===== 常驻监听启动（间隔 {interval}s，联系人「{cfg.get('contact')}」）=====")
    log(f"模型: {reply_engine.llm_cfg(cfg).get('model')} @ {reply_engine.llm_cfg(cfg).get('base_url')}")
    if not auto_send_enabled(cfg):
        log("自动发送已关闭（草稿模式）：只生成 data/reply.txt，不操作微信")

    while True:
        try:
            reason = loop_once(cfg, args, state)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"本轮异常（已忽略，继续）: {type(e).__name__}: {e}")
            reason = ""
        if args.once:
            log("--once：本轮结束")
            break
        if stop_requested():
            log("收到停止请求，退出常驻监听")
            break
        time.sleep(interval)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮检测（测试用）")
    ap.add_argument("--no-send", action="store_true", help="只生成回复不发送（测试用）")
    ap.add_argument("--interval", type=int, default=0, help="轮询间隔秒（默认取 config.poll_seconds）")
    ap.add_argument("--context", type=int, default=16, help="带给模型的上下文条数")
    ap.add_argument("--pending-retry", type=int, default=300,
                    help="上轮没发成功的消息，至少隔这么多秒再重试（默认 300）")
    ap.add_argument("--force", action="store_true", help="忽略「已有实例在跑」直接启动")
    ap.add_argument("--test-llm", action="store_true", help="只用一句假消息试一次模型")
    ap.add_argument("--status", action="store_true", help="打印运行状态")
    ap.add_argument("--stop", action="store_true", help="让正在运行的常驻进程停止")
    ap.add_argument("--init-baseline", action="store_true",
                    help="把当前聊天记录设为基线（视为已读），不回复历史消息")
    args = ap.parse_args()

    if args.init_baseline:
        ns = _Ns()
        ns.clear_pending = False
        ns.init = True
        ns.context = 0
        ns.out = ""
        ns.json = False
        ns.text = False
        ns._quiet = False
        code, res = monitor.run_once(ns)
        print(json.dumps(res or {"ok": False}, ensure_ascii=False, indent=2))
        return 0 if code == 0 else 1

    if args.stop:
        ok = request_stop("cli")
        print(json.dumps({"ok": ok, "stopped": self_lock_alive()}, ensure_ascii=False))
        log("收到 --stop：已写入停止标记" if ok else "--stop：写停止标记失败")
        return 0 if ok else 1

    cfg = monitor.load_json(CONFIG_PATH, {})

    if args.status:
        s = monitor.load_json(STATS_PATH, {})
        print(json.dumps({
            "daemon_running": self_lock_alive(),
            "daemon_lock": monitor.load_json(DAEMON_LOCK, {}),
            "stats": s,
            "pending": len(monitor.load_pending()),
            "llm_configured": bool(reply_engine.llm_cfg(cfg).get("api_key")),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.test_llm:
        demo = {
            "contact": cfg.get("contact"),
            "context": [{"from_her": True, "text": "今天好累"}, {"from_her": False, "text": "咋了"}],
            "new_messages": [{"text": "被领导骂了 烦死了"}],
            "new_images": [],
        }
        try:
            lines, meta = reply_engine.generate(cfg, demo)
            print(json.dumps({"ok": True, "lines": lines, "meta": meta},
                             ensure_ascii=False, indent=2))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
            return 1
        return 0

    if not args.force and self_lock_alive():
        log("已有 daemon 在运行（心跳正常），本次退出。要强启加 --force")
        return 13

    # 全局互斥：exe 版和源码版的数据目录不同，各自的 daemon.lock 看不见对方，
    # 必须靠这把公共锁拦住「两个一起跑」
    other = global_lock_alive()
    if other and not args.force:
        log("已有另一个实例在运行 —— %s版，pid=%s，数据目录 %s"
            % (other.get("source", "?"), other.get("pid"), other.get("app_dir", "?")))
        log("两个一起跑会重复回复、还会抢微信窗口（发送会变得极慢）。"
            "请先停掉那一个，或加 --force 强行启动")
        return 13

    self_lock_touch()
    try:
        supervise(cfg, args)
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        self_lock_release()
        monitor.lock_release()
    return 0


if __name__ == "__main__":
    # 用 pythonw.exe 起时没有控制台，任何没抓住的异常都会静默消失、进程直接没了。
    # 所以顶层兜一层，把 traceback 落到 data/daemon_crash.log，方便事后查。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        try:
            with open(os.path.join(DATA_DIR, "daemon_crash.log"), "a", encoding="utf-8") as _f:
                _f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] pid={os.getpid()}\n")
                _f.write(traceback.format_exc())
        except Exception:
            pass
        raise
