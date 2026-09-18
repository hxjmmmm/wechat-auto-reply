# -*- coding: utf-8 -*-
"""
verify_sent.py — 发送后校验：确认消息真的落在了她的会话里

为什么需要它（替代原来的截图 OCR 校验）：
    截图 OCR 是整条链路里唯一依赖屏幕的环节，也是息屏时发送失败的唯一原因。
    而它做的事只是「模糊比对窗口标题里有没有那四个字」，既不准又贵。

    这里换成**直接读数据库**：查她的消息表 `Msg_<md5(她的wxid)>`，
    看刚发出去的那几句话是不是真的出现在里面。
    - 命中 → 100% 确定发对人了
    - 没命中 → 说明导航进了别的会话，消息发错地方了，必须告警

    完全离线，不碰屏幕，比 OCR 精确得多。

用法：
    python verify_sent.py --texts-file data/reply.txt --minutes 5 --out data/sent_check.json
    python verify_sent.py --texts "在呢|咋啦" --minutes 3

退出码：0 = 全部命中；1 = 有未命中的（发错会话了）；2 = 脚本出错
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta

# 输出统一成 UTF-8（不能用 io.TextIOWrapper 包一层：无控制台时 sys.stdout 是 None，
# 摸 .buffer 会崩，包装器还会在解释器退出时先关掉底层流）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _wechat_cli_dirs():
    """wechat_cli 包可能在的目录（打包后已内置，源码跑时按已知位置补）"""
    out = []
    try:
        import runner
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            out.append(meipass)
        app = runner.app_dir()
        out += [app, os.path.join(app, "scripts"), os.path.dirname(os.path.abspath(__file__))]
    except Exception:
        pass
    # 本机装了 wechat-local-butler 技能时的位置。
    # 目录名按**当前登录用户名**推导 —— 以前写死成某个人的家目录，换台机器就找不到。
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user:
        for home in (".workbuddy", ".codebuddy"):
            out.append(os.path.join("C:\\Users", user, home, "skills",
                                    "wechat-local-butler", "tool"))
    home_dir = os.path.expanduser("~")
    for home in (".workbuddy", ".codebuddy"):
        out.append(os.path.join(home_dir, home, "skills", "wechat-local-butler", "tool"))
    seen, uniq = set(), []
    for p in out:
        if p and os.path.isdir(p) and p.lower() not in seen:
            seen.add(p.lower())
            uniq.append(p)
    return uniq


def _ensure_wechat_cli():
    """保证 wechat_cli 可导入。

    以前这里写死 C:\\Users\\<某人>\\.workbuddy\\... 的绝对路径 ——
    换台机器（或技能目录挪走）就直接 ImportError，校验恒失败。
    现在先直接试导入，失败再按候选目录补 sys.path。
    """
    try:
        import wechat_cli  # noqa: F401
        return True
    except Exception:
        pass
    for p in _wechat_cli_dirs():
        if p not in sys.path:
            sys.path.insert(0, p)
        try:
            import wechat_cli  # noqa: F401
            return True
        except Exception:
            continue
    return False

def _app_dir():
    """配置与数据的根目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录"""
    try:
        import runner
        return runner.app_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


HERE = _app_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def load_cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def to_text(val):
    """WCDB 的列可能是 str 也可能是 zstd 压缩过的 bytes"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (bytes, bytearray)):
        if bytes(val[:4]) == ZSTD_MAGIC:
            try:
                import zstandard
                return zstandard.ZstdDecompressor().decompress(bytes(val)).decode("utf-8", "replace")
            except Exception:
                return ""
        try:
            return bytes(val).decode("utf-8", "replace")
        except UnicodeDecodeError:
            return ""
    return str(val)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", default="", help="待校验文本，多条用 | 分隔")
    ap.add_argument("--texts-file", default="", help="待校验文本文件，每行一条")
    ap.add_argument("--minutes", type=int, default=5, help="只看最近 N 分钟（默认 5）")
    ap.add_argument("--out", default="", help="结果写入 UTF-8 文件")
    args = ap.parse_args()

    lines = []
    if args.texts_file and os.path.exists(args.texts_file):
        with open(args.texts_file, encoding="utf-8") as f:
            lines = [x.strip() for x in f.read().split("\n") if x.strip()]
    if not lines and args.texts:
        lines = [x.strip() for x in args.texts.split("|") if x.strip()]
    lines = [x for x in lines if x]

    result = {"ok": False, "checked": lines, "matched": [], "missing": [],
              "recent": [], "error": None}

    if not lines:
        result["error"] = "没有待校验的文本"
        _emit(result, args.out)
        return 2

    cfg = load_cfg()
    her = cfg.get("contact_wxid", "")
    me = cfg.get("my_wxid", "")

    try:
        if not _ensure_wechat_cli():
            result["error"] = ("导入 wechat_cli 失败：找不到技能目录。"
                               "打包版应内置该库；源码版请先运行环境检测或手动指定 decrypt_python")
            _emit(result, args.out)
            return 2
        from wechat_cli.core.context import AppContext
        from wechat_cli.core.messages import _find_msg_tables_for_user

        ctx = AppContext()
        tables = _find_msg_tables_for_user(her, ctx.msg_db_keys, ctx.cache)
        if not tables:
            result["error"] = f"找不到 {her} 的消息表"
            _emit(result, args.out)
            return 2

        since = int((datetime.now() - timedelta(minutes=args.minutes)).timestamp())
        my_id = None
        got = []
        for t in tables:
            table = t["table_name"]
            if not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
                continue
            conn = sqlite3.connect(t["db_path"])
            try:
                if me:
                    r = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (me,)).fetchone()
                    my_id = r[0] if r else None
                rows = conn.execute(
                    f"SELECT local_id, create_time, real_sender_id, message_content "
                    f"FROM [{table}] WHERE create_time >= ? ORDER BY create_time DESC LIMIT 40",
                    (since,)).fetchall()
                for local_id, ct, sid, content in rows:
                    txt = to_text(content)
                    if not txt:
                        continue
                    got.append({"ts": ct, "sender_is_me": (my_id is not None and sid == my_id),
                                "text": txt})
            finally:
                conn.close()

        got.sort(key=lambda x: -x["ts"])
        result["recent"] = [
            {"time": datetime.fromtimestamp(g["ts"]).strftime("%H:%M:%S"),
             "me": g["sender_is_me"], "text": g["text"][:60]} for g in got[:10]
        ]

        # 只看「我发的」那些；拿不到 me 的 rowid 就退化成全量比对
        mine = [g["text"] for g in got if g["sender_is_me"]] if my_id is not None \
            else [g["text"] for g in got]
        for line in lines:
            if any(line == m.strip() or line in m for m in mine):
                result["matched"].append(line)
            else:
                result["missing"].append(line)
        result["ok"] = not result["missing"]
    except Exception as e:
        # 只报「FileNotFoundError」这种类型+消息是没法排查的 —— 不知道是哪个文件、
        # 哪一行。把堆栈一起带出去，上层日志里就能一眼看到根因。
        try:
            import traceback
            tb = traceback.format_exc(limit=8)
        except Exception:
            tb = ""
        result["error"] = f"{type(e).__name__}: {e}" + (("\n" + tb) if tb else "")
        result["traceback"] = tb

    _emit(result, args.out)
    return 0 if result["ok"] else 1


def _emit(result, out):
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    if result["error"]:
        print("校验出错: %s" % result["error"])
    elif result["missing"]:
        print("⚠ 未落在该会话: %s" % " | ".join(result["missing"]))
    else:
        print("✓ 已确认落在她的会话: %s" % " | ".join(result["matched"]))


if __name__ == "__main__":
    sys.exit(main())
