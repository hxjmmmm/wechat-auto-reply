# -*- coding: utf-8 -*-
"""
voice_text.py — 读取目标联系人语音消息的转写文字

背景（重要）：
    语音的音频文件确实不落盘（VoiceTemp 会被清空，attach 里根本没有 Voice 目录），
    但**微信「语音转文字」的结果是落盘的** —— 存在消息表的 `packed_info_data`
    字段里，protobuf 编码。之前全盘搜索搜不到，是因为数据库文件本身是加密的，
    明文只存在于解密之后。

    本脚本复用 wechat-cli 的解密能力拿到明文库，然后直接查消息表，
    把 local_type=34（语音）且 real_sender_id = 她 的记录的转写文本提取出来。

    消息表结构（微信 4.x）：
        Msg_<md5(对方wxid)>  (local_id, server_id, local_type, sort_seq,
                              real_sender_id, create_time, status, ...,
                              message_content, compress_content,
                              packed_info_data, WCDB_CT_message_content, ...)
    packed_info_data 实测结构：
        08 16 | 10 02 | 2a <len> [ 08 02 | 12 <len> <UTF-8 转写文本> ] | 58 00

用法：
    python voice_text.py --minutes 60
    python voice_text.py --at "2026-09-15 17:31" --at "2026-09-15 18:02"
    python voice_text.py --minutes 120 --out data/voice_text.json

注意：必须用 wechat-cli 那个 venv 的 python 跑（需要 wechat_cli + zstandard），
      即 config.json 里的 decrypt_python。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

# 输出统一成 UTF-8（不用 io.TextIOWrapper 包一层：无控制台时 sys.stdout 为 None 会崩，
# 且包装器会在解释器退出时先关掉底层流，再 flush 就报 I/O on closed file）
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
CONFIG_PATH = os.path.join(HERE, "config.json")

CJK = re.compile(r"[\u4e00-\u9fff]")
# 控制字符：protobuf 的长度前缀/字段头经常恰好落在 ASCII 可打印或控制区，
# 会被误判成「一段合法 UTF-8」，所以用它把脏候选刷掉。
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------------- 极简 protobuf 解析（只为捞出转写文本，不引入依赖） ----------------

def _read_varint(b, i):
    r = 0
    s = 0
    while i < len(b):
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7
        if s > 63:
            break
    return None, i


def _fields(b):
    """返回 [(field_no, bytes_value), ...]，只取 length-delimited 字段"""
    out = []
    i = 0
    while i < len(b):
        key, j = _read_varint(b, i)
        if key is None:
            break
        wt = key & 7
        if wt == 2:
            ln, k = _read_varint(b, j)
            if ln is None or k + ln > len(b):
                break
            out.append((key >> 3, b[k:k + ln]))
            i = k + ln
        elif wt == 0:
            _, i = _read_varint(b, j)
        elif wt == 5:
            i = j + 4
        elif wt == 1:
            i = j + 8
        else:
            break
    return out


def _collect(b, depth, out):
    for _fld, val in _fields(b):
        try:
            out.append(val.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        if depth < 3:
            _collect(val, depth + 1, out)


def extract_text(b):
    """递归收集所有能解成 UTF-8 的串，取「含中文且无控制字符」里最长的一个"""
    cands = []
    _collect(b, 0, cands)
    clean = [s for s in cands if CJK.search(s) and not CTRL.search(s)]
    if clean:
        return max(clean, key=len)
    loose = [s for s in cands if CJK.search(s) and "\x00" not in s]
    return max(loose, key=len) if loose else None


# ---------------- 数据库访问 ----------------

def load_cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def open_context():
    from wechat_cli.core.context import AppContext
    return AppContext()


def find_tables(ctx, username):
    from wechat_cli.core.messages import _find_msg_tables_for_user
    return _find_msg_tables_for_user(username, ctx.msg_db_keys, ctx.cache)


def sender_id_for(conn, username):
    """Name2Id 里查出该 wxid 对应的 rowid"""
    try:
        row = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (username,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def fetch_voices(ctx, username, since_ts, limit=50):
    tables = find_tables(ctx, username)
    if not tables:
        return [], "未找到该联系人的消息表"

    out = []
    for t in tables:
        db_path = t["db_path"]
        table = t["table_name"]
        if not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
            continue
        conn = sqlite3.connect(db_path)
        try:
            sid = sender_id_for(conn, username)
            sql = (f"SELECT local_id, create_time, packed_info_data "
                   f"FROM [{table}] WHERE local_type=34")
            params = []
            if since_ts:
                sql += " AND create_time >= ?"
                params.append(since_ts)
            if sid is not None:
                sql += " AND real_sender_id = ?"
                params.append(sid)
            sql += " ORDER BY create_time DESC LIMIT ?"
            params.append(limit)
            for local_id, ct, pid in conn.execute(sql, params):
                if not pid:
                    continue
                if isinstance(pid, str):
                    pid = pid.encode("utf-8", "ignore")
                text = extract_text(pid)
                if not text:
                    continue
                out.append({
                    "local_id": local_id,
                    "ts": ct,
                    "time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M"),
                    "text": text,
                })
        except sqlite3.Error as e:
            return out, f"查询失败: {e}"
        finally:
            conn.close()
    out.sort(key=lambda x: x["ts"])
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60, help="取最近 N 分钟（默认 60）")
    ap.add_argument("--at", action="append", default=[],
                    help="指定时刻 'YYYY-MM-DD HH:MM'，可重复；命中其前后 3 分钟的语音")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--out", default="", help="结果写入 UTF-8 文件")
    args = ap.parse_args()

    cfg = load_cfg()
    username = cfg.get("contact_wxid", "")

    result = {"ok": False, "contact": cfg.get("contact"), "voices": [], "error": None}
    try:
        ctx = open_context()
        if args.at:
            seen = {}
            for s in args.at:
                try:
                    base = datetime.strptime(s, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                vs, err = fetch_voices(ctx, username,
                                       int((base - timedelta(minutes=3)).timestamp()),
                                       args.limit)
                if err:
                    result["error"] = err
                for v in vs:
                    if abs((datetime.fromtimestamp(v["ts"]) - base).total_seconds()) <= 180:
                        seen[v["local_id"]] = v
            result["voices"] = sorted(seen.values(), key=lambda x: x["ts"])
        else:
            since = int((datetime.now() - timedelta(minutes=args.minutes)).timestamp())
            vs, err = fetch_voices(ctx, username, since, args.limit)
            result["voices"] = vs
            result["error"] = err
        result["ok"] = not result["error"]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    if result["voices"]:
        for v in result["voices"]:
            print(f"[{v['time']}] {v['text']}")
    else:
        print("（该时间窗内没有可读取的语音转写）"
              + (f" 错误：{result['error']}" if result["error"] else ""))

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
