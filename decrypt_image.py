# -*- coding: utf-8 -*-
"""
decrypt_image.py — 解密微信 4.x 聊天图片（V2 格式），供自动化"看图"用

原理（实测微信 4.1.13 有效）：
  V2 .dat 结构：
    [0:6]   魔数 07 08 56 32 08 07 ("\\x07\\x08V2\\x08\\x07")
    [6:10]  aes_size  (uint32 LE，实测恒为 1024)
    [10:14] xor_size  (uint32 LE)
    [14]    标志字节 0x01
    [15:15+aes_size]        AES-128-ECB 密文（图像头部）
    [15+aes_size : +16]     分隔尾，跳过
    [+16 : +xor_size]       单字节 XOR 混淆的剩余部分
  总长公式：file_size == 15 + aes_size + 16 + xor_size

  账号级密钥（离线派生，不用碰微信进程）：
    code    从 %APPDATA%\\Tencent\\xwechat\\net\\kvcomm\\key_<code>_*.statistic 文件名提取
    wxid    对方/自己的 wxid，去掉 "_数字" 后缀
    aes_key = md5(f"{code}{wxid}").hexdigest()[:16]   ← ASCII 前16字符直接当密钥
    xor_key = code & 0xFF

  会话图片目录 = msg\\attach\\<md5(对方wxid)>\\<年-月>\\Img\\
  同一张图最多三份：<md5>_h.dat(高清) > <md5>.dat(显示版) > <md5>_t.dat(缩略图)

用法：
  # 按消息时间精确取图（主路径：检测到几条图片消息就传几个 --at）
  python decrypt_image.py --at "2026-09-15 17:31" --at "2026-09-15 15:26" --out result.json
  # 或按"最近 N 分钟"批量取图
  python decrypt_image.py --minutes 10 --max 4 --out result.json

输出：JSON（含解密后的图片绝对路径 + 对应的消息时间），供 agent 用 Read 工具查看

依赖：pycryptodome（AES 解密）。须用装了它的解释器运行，见 config.json 的 decrypt_python。
"""
import argparse
import ctypes
import glob
import hashlib
import json
import os
import re
import struct
import sys
import time
from datetime import datetime

# 输出统一成 UTF-8（不用 io.TextIOWrapper 包一层：无控制台时 sys.stdout 为 None 会崩，
# 且包装器会在解释器退出时先关掉底层流，再 flush 就报 I/O on closed file）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from Crypto.Cipher import AES

def _app_dir():
    """配置与数据的根目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录"""
    try:
        import runner
        return runner.app_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


HERE = _app_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

V2_MAGIC = b"\x07\x08V2\x08\x07"


def load_cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_code(cfg=None):
    """从 MMKV 统计文件名里提取账号 code。

    以前这里用宽松正则 `key_(\\d+)_` 在 os.listdir 的任意顺序里碰运气，
    实际命中过一个 `key_0_4065598785_...` 的无关文件 → code=0 → xor_key=0、
    aes_key 也跟着错，解出来全是垃圾。现在两级保险：

    1) 优先用 config 里已探测好的 account_code；
    2) 否则复用 wxenv.find_account_code()（严格正则 + 按时间戳排序，选当前登录账号），
       保证和界面探测的口径一致；
    3) 最后才扫目录，且 0 不是合法 code，直接跳过。
    """
    code = str((cfg or {}).get("account_code") or "").strip()
    if code.isdigit() and int(code) > 0:
        return int(code), "config.json 的 account_code"

    try:
        import wxenv
        c, _codes = wxenv.find_account_code()
        if c and str(c).isdigit() and int(c) > 0:
            return int(c), "wxenv 自动探测"
    except Exception:
        pass

    appdata = os.environ.get("APPDATA", "")
    roots = [
        os.path.join(appdata, "Tencent", "xwechat", "net", "kvcomm"),
        os.path.join(appdata, "Tencent", "xwechat", "net", "ilink", "kvcomm"),
    ]
    pat = re.compile(r"^key_(\d+)_")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            m = pat.match(fn)
            if not m:
                continue
            v = int(m.group(1))
            if v > 0:
                return v, os.path.join(root, fn)
    return None, None


def clean_wxid(wxid):
    """wxid_xxxxxxxxxxxx_c382 -> wxid_xxxxxxxxxxxx（示例，不代表任何真实账号）"""
    parts = wxid.split("_")
    if wxid.startswith("wxid_") and len(parts) >= 3:
        return "_".join(parts[:2])
    return wxid


def detect_format(head):
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"\x89PNG":
        return "png"
    if head[:3] == b"GIF":
        return "gif"
    if head[:4] == b"RIFF":
        return "webp"
    if head[:4] == b"wxgf":
        return "wxgf"
    return "bin"


def decrypt_v2(data, aes_key, xor_key):
    if data[:6] != V2_MAGIC:
        raise ValueError("not v2")
    aes_size = struct.unpack("<I", data[6:10])[0]
    xor_size = struct.unpack("<I", data[10:14])[0]
    head = AES.new(aes_key, AES.MODE_ECB).decrypt(data[15:15 + aes_size])
    off = 15 + aes_size + 16
    tail = bytes(b ^ xor_key for b in data[off:off + xor_size])
    return head + tail, aes_size, xor_size


def find_voip_dll():
    """定位微信自带的 wxgf 解码 DLL（按版本号倒序取最新）"""
    bases = [
        r"D:\Weixin",
        r"C:\Program Files\Tencent\Weixin",
        r"C:\Program Files (x86)\Tencent\Weixin",
    ]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        bases.append(os.path.join(la, "Tencent", "Weixin"))
    cands = []
    for b in bases:
        if not b or not os.path.isdir(b):
            continue
        cands += glob.glob(os.path.join(b, "*", "VoipEngine.dll"))
        d = os.path.join(b, "VoipEngine.dll")
        if os.path.exists(d):
            cands.append(d)
    if not cands:
        return None

    def ver(p):
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    cands.sort(key=ver, reverse=True)
    return cands[0]


_wxgf_lib = None

# 第 5 个参数是 32 字节配置缓冲区，不能传 NULL（传 NULL 会 access violation）
# 内容来自社区逆向结果，中间的 AA AA AA AA 是标志位
_WXGF_ARG5 = bytes.fromhex(
    "00 00 00 00 00 00 00 00 00 00 00 00 AA AA AA AA"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
)


def convert_wxgf(data, dll_path):
    """调 VoipEngine.dll 的 wxam_dec_wxam2pic_5 把 wxgf 解码并重编码为 JPEG"""
    global _wxgf_lib
    try:
        if _wxgf_lib is None:
            os.add_dll_directory(os.path.dirname(dll_path))
            _wxgf_lib = ctypes.WinDLL(dll_path)

        raw_array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        plain_array = (ctypes.c_ubyte * (8 << 20))()
        # 注意：这里是"容量"初值，不是 0 —— 传 0 会拿不到数据
        plain_size = ctypes.c_uint32(0x7CFB00)
        arg5 = (ctypes.c_ubyte * len(_WXGF_ARG5)).from_buffer_copy(_WXGF_ARG5)

        _wxgf_lib.wxam_dec_wxam2pic_5(
            raw_array, ctypes.c_uint32(len(data)),
            plain_array, ctypes.byref(plain_size), arg5)

        n = int(plain_size.value)
        if n <= 0 or n > len(plain_array):
            return None
        return bytes(plain_array[:n])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10, help="只处理最近 N 分钟内新增的图片")
    ap.add_argument("--at", action="append", default=[],
                    help="消息时间 'YYYY-MM-DD HH:MM[:SS]'，在该时刻附近取图（可重复传，与消息一一对应）")
    ap.add_argument("--window", type=float, default=300, help="--at 的时间容差（秒）")
    ap.add_argument("--out-dir", default=os.path.join(DATA_DIR, "images"))
    ap.add_argument("--out", default="", help="把 JSON 结果写入该文件")
    ap.add_argument("--max", type=int, default=4, help="最多解密几张")
    ap.add_argument("--keep-hours", type=float, default=24, help="清理 out-dir 中 N 小时前的旧图")
    args = ap.parse_args()

    result = {"ok": False, "images": [], "skipped": [], "error": None}
    try:
        cfg = load_cfg()
        db_dir = cfg.get("db_dir", "")
        # db_dir 形如 ...\wxid_xxx_c382\db_storage  → 取账号根目录
        acc_root = os.path.dirname(db_dir)
        attach = os.path.join(acc_root, "msg", "attach")

        peer = cfg.get("contact_wxid") or cfg.get("peer_wxid")
        own = os.path.basename(acc_root)

        # 优先用对方 wxid 定位会话目录
        session_md5 = hashlib.md5(peer.encode()).hexdigest()
        img_dir_candidates = [os.path.join(attach, session_md5)]

        code, code_src = find_code(cfg)
        result["code"] = code
        result["code_source"] = code_src
        if code is None:
            result["error"] = "未能从 MMKV 文件名提取 code（图片密钥来源）"
            _emit(result, args)
            return 1

        aes_key = hashlib.md5(f"{code}{clean_wxid(own)}".encode()).hexdigest()[:16].encode()
        xor_key = code & 0xFF
        result["aes_key_preview"] = aes_key.decode()
        result["xor_key"] = xor_key

        # 会话目录下所有 .dat 及其 mtime
        all_files = []
        for d in img_dir_candidates:
            for p in glob.glob(os.path.join(d, "**", "*.dat"), recursive=True):
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                all_files.append((mt, p))

        def rank_of(path):
            base = os.path.basename(path)[:-4]
            stem = base[:-2] if base.endswith(("_h", "_t")) else base
            rank = 0 if base.endswith("_h") else (1 if not base.endswith("_t") else 2)
            return base, stem, rank

        # 同一张图的多档位去重：_h(高清) > 无后缀(显示版) > _t(缩略图)
        dedup = {}
        for mt, p in all_files:
            base, stem, rank = rank_of(p)
            if stem not in dedup or rank < dedup[stem][0]:
                dedup[stem] = (rank, mt, p)

        ordered = []          # [(rank, mtime, path, matched_at)]
        used_stems = set()

        if args.at:
            # 主路径：按消息时间逐个就近匹配（检测到几条图片消息就解几张）
            for spec in args.at:
                spec = (spec or "").strip()
                t = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        t = time.mktime(datetime.strptime(spec, fmt).timetuple())
                        break
                    except ValueError:
                        continue
                if t is None:
                    result["skipped"].append({"at": spec, "reason": "时间格式无法解析"})
                    continue
                cands = []
                for stem, (rank, mt, p) in dedup.items():
                    if stem in used_stems:
                        continue
                    delta = abs(mt - t)
                    if delta <= args.window:
                        cands.append((delta, rank, stem, mt, p))
                if not cands:
                    result["skipped"].append({"at": spec, "reason": "该时刻附近没有图片文件"})
                    continue
                cands.sort(key=lambda x: (x[0], x[1]))
                delta, rank, stem, mt, p = cands[0]
                used_stems.add(stem)
                ordered.append((rank, mt, p, spec))
        else:
            cutoff = time.time() - args.minutes * 60
            recent = [v for v in dedup.values() if v[1] >= cutoff]
            recent.sort(key=lambda x: x[1], reverse=True)
            ordered = [(r, mt, p, "") for r, mt, p in recent[:args.max]]

        os.makedirs(args.out_dir, exist_ok=True)
        # 清理旧图，避免 data\images 无限膨胀
        if args.keep_hours > 0:
            expire = time.time() - args.keep_hours * 3600
            for old in glob.glob(os.path.join(args.out_dir, "*")):
                try:
                    if os.path.isfile(old) and os.path.getmtime(old) < expire:
                        os.remove(old)
                except OSError:
                    pass

        voip = find_voip_dll()
        result["wxgf_dll"] = voip
        for rank, mt, p, matched_at in ordered:
            try:
                data = open(p, "rb").read()
                out, aes_size, xor_size = decrypt_v2(data, aes_key, xor_key)
                fmt = detect_format(out[:8])
                converted = False
                if fmt == "wxgf" and voip:
                    conv = convert_wxgf(out, voip)
                    if conv:
                        out = conv
                        fmt = detect_format(out[:8])
                        if fmt == "bin":
                            fmt = "jpg"
                        converted = True
                # 解完还是认不出格式 = 密钥不对（多半是账号 code 取错了）。
                # 这种必须丢掉 —— 以前照样写盘并当成"成功的图片"上报，
                # 结果视觉模型收到一堆垃圾，直接 4xx，整轮回复卡死。
                if fmt not in ("jpg", "png", "gif", "webp"):
                    result["skipped"].append(
                        {"file": p, "reason": f"解密后不是可识别的图片（{fmt}），多半是密钥不对"})
                    continue
                name = "%s.%s" % (os.path.splitext(os.path.basename(p))[0], fmt)
                dst = os.path.join(args.out_dir, name)
                with open(dst, "wb") as f:
                    f.write(out)
                result["images"].append({
                    "path": dst,
                    "source": p,
                    "matched_at": matched_at,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt)),
                    "quality": {0: "hd", 1: "normal", 2: "thumb"}[rank],
                    "format": fmt,
                    "wxgf_converted": converted,
                    "bytes": len(out),
                })
            except Exception as e:
                result["skipped"].append({"file": p, "reason": repr(e)})

        result["ok"] = bool(result["images"])
    except Exception as e:
        result["error"] = repr(e)

    _emit(result, args)
    return 0 if result["ok"] else 1


def _emit(result, args):
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
