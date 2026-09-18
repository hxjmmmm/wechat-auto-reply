# -*- coding: utf-8 -*-
"""wxenv.py — 自动探测本机微信环境（账号 / 数据目录 / 安装路径 / 依赖工具）

设计目标：用户不需要手填任何路径。打开工具点一下「自动检测」，
下面这些全部自动填好：

    微信安装目录 + 版本      ← 注册表 HKCU\\Software\\Tencent\\Weixin
    图片解密用的 DLL 目录     ← 安装目录下的版本子目录（含 VoipEngine.dll）
    账号 code（图片解密密钥） ← %APPDATA%\\Tencent\\xwechat\\net\\kvcomm\\key_<code>_*.statistic
    微信数据根目录            ← 各盘 Documents 下的 xwechat_files
    登录过的账号列表          ← 数据根目录下的 wxid_* 子目录
    wechat-cli 路径           ← 已知技能目录 + PATH

只用标准库。所有探测都是「只读」，不改动微信任何文件。
"""
import glob
import json
import os
import re
import subprocess
import sys

import runner          # 子进程统一 CREATE_NO_WINDOW（防无窗口 exe 下闪黑框）

IS_WIN = sys.platform.startswith("win")


def app_dir():
    """配置与数据的根目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录"""
    try:
        import runner
        return runner.app_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


# WeChat 4.x 注册表位置（3.x 是 WeChat，4.x 是 Weixin，两个都试）
REG_PATHS = [
    (r"Software\Tencent\Weixin", "HKCU"),
    (r"Software\Tencent\WeChat", "HKCU"),
    (r"Software\WOW6432Node\Tencent\Weixin", "HKLM"),
    (r"Software\WOW6432Node\Tencent\WeChat", "HKLM"),
    (r"Software\Tencent\Weixin", "HKLM"),
    (r"Software\Tencent\WeChat", "HKLM"),
]

# wechat-cli 可能出现的地方（本机装了 wechat-local-butler 技能的话就在这）
CLI_CANDIDATES = [
    r"C:\Users\{user}\.workbuddy\skills\wechat-local-butler\tool\.venv\Scripts\wechat-cli.exe",
    r"C:\Users\{user}\.workbuddy\skills\wechat-local-butler\tool\wechat-cli.exe",
    r"C:\Users\{user}\.codebuddy\skills\wechat-local-butler\tool\.venv\Scripts\wechat-cli.exe",
    r".venv\Scripts\wechat-cli.exe",
    r"tool\.venv\Scripts\wechat-cli.exe",
]

SEND_CANDIDATES = [
    r"C:\Users\{user}\.workbuddy\skills\wechat-send-new\scripts\.venv\Scripts\python.exe",
    r".venv\Scripts\python.exe",
]


def _user():
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


# ---------------------------------------------------------------- 注册表

def read_reg():
    """读微信安装信息。返回 {install_path, version, raw:{...}}"""
    if not IS_WIN:
        return {}
    try:
        import winreg
    except ImportError:
        return {}

    out = {}
    for sub, hive in REG_PATHS:
        root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        try:
            with winreg.OpenKey(root, sub) as k:
                vals = {}
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    vals[name] = val
                    i += 1
        except OSError:
            continue
        if not vals:
            continue
        out.setdefault("raw", {})[f"{hive}\\{sub}"] = vals
        ip = vals.get("InstallPath") or vals.get("Install Dir") or ""
        ver = vals.get("Version") or vals.get("VersionNum") or ""
        if ip and not out.get("install_path"):
            out["install_path"] = ip
        if ver and not out.get("version"):
            out["version"] = str(ver)
    return out


def version_dirs(install_path):
    """安装目录下的版本子目录（形如 4.1.13.65），按版本号从大到小"""
    if not install_path or not os.path.isdir(install_path):
        return []
    dirs = []
    for name in os.listdir(install_path):
        p = os.path.join(install_path, name)
        if os.path.isdir(p) and re.match(r"^\d+(\.\d+){1,3}$", name):
            dirs.append(([int(x) for x in name.split(".")], name, p))
    dirs.sort(key=lambda x: x[0], reverse=True)
    return [(n, p) for _, n, p in dirs]


def find_voip_dir(install_path):
    """含 VoipEngine.dll 的目录（图片解密必须）"""
    for name, p in version_dirs(install_path):
        if os.path.exists(os.path.join(p, "VoipEngine.dll")):
            return p, name
    # 有的版本 DLL 就放在根目录
    if install_path and os.path.exists(os.path.join(install_path, "VoipEngine.dll")):
        return install_path, ""
    return "", ""


# ---------------------------------------------------------------- 账号 code

KEY_RE = re.compile(r"^key_(\d+)_\d+_\d+_\d+_\d+_\d+_(?:input|output)\.statistic$", re.I)


def find_account_code():
    """从 kvcomm 的 key_<code>_*.statistic 文件名提取账号 code（图片解密密钥的一部分）

    同一台机可能登录过多个账号，按文件名里的时间戳排序取最新的那个
    （也就是当前登录的）。返回 (code, 全部候选列表)
    """
    appdata = os.environ.get("APPDATA") or ""
    kv = os.path.join(appdata, "Tencent", "xwechat", "net", "kvcomm")
    cands = []
    if os.path.isdir(kv):
        for name in os.listdir(kv):
            m = KEY_RE.match(name)
            if not m:
                continue
            ts = 0
            parts = name.split("_")
            for seg in parts:
                if seg.isdigit() and len(seg) >= 9:      # 10 位时间戳
                    ts = max(ts, int(seg[:10]))
            cands.append((ts, m.group(1)))
    cands.sort(reverse=True)
    codes = []
    for _, c in cands:
        if c not in codes:
            codes.append(c)
    return (codes[0] if codes else ""), codes


# ---------------------------------------------------------------- 数据目录

def _documents_dirs():
    """所有可能的「文档」目录（微信默认把 xwechat_files 放这里，但可能被改到 D 盘）"""
    out = []

    # 1) 注册表里的 Personal（用户可能把文档重定向到别的盘）
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                val, _ = winreg.QueryValueEx(k, "Personal")
                out.append(os.path.expandvars(val))
        except OSError:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
                val, _ = winreg.QueryValueEx(k, "Personal")
                out.append(val)
        except OSError:
            pass

    # 2) 常规位置
    up = os.environ.get("USERPROFILE") or ""
    if up:
        out.append(os.path.join(up, "Documents"))
        out.append(os.path.join(up, "文档"))

    # 3) 各盘根下的常见布局（微信换过默认位置，有人是 D:\Users\Documents）
    for d in "CDEFGH":
        out.append(f"{d}:\\Users\\{_user()}\\Documents")
        out.append(f"{d}:\\Users\\Documents")
        out.append(f"{d}:\\Documents")

    seen, uniq = set(), []
    for p in out:
        if not p:
            continue
        k = p.lower().rstrip("\\")
        if k in seen or not os.path.isdir(p):
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def find_data_root():
    """找 xwechat_files 根目录（微信所有账号数据都在这下面）"""
    hits = []
    for doc in _documents_dirs():
        try:
            entries = os.listdir(doc)
        except OSError:
            continue
        for n in entries:
            if n.lower().startswith("xwechat_files"):
                p = os.path.join(doc, n)
                if os.path.isdir(p):
                    hits.append(p)
    return hits


def list_accounts(data_root):
    """数据根目录下的账号（每个子目录是一个登录过的账号）

    返回 [{"wxid":.., "db_dir":.., "mtime":.., "size_hint":..}]
    """
    out = []
    if not data_root or not os.path.isdir(data_root):
        return out
    for n in os.listdir(data_root):
        p = os.path.join(data_root, n)
        if not os.path.isdir(p) or n.startswith("."):
            continue
        # 排除 Backup / all_users 这类公共目录 —— 不是账号
        if not n.lower().startswith("wxid_") and not n.lower().startswith("gh_"):
            if not os.path.isdir(os.path.join(p, "db_storage")):
                continue
        db = os.path.join(p, "db_storage")
        mtime = 0.0
        try:
            mtime = os.path.getmtime(db if os.path.isdir(db) else p)
        except OSError:
            pass
        out.append({
            "dir_name": n,
            # 目录名是 wxid_xxxx_<4位后缀>，真正的 wxid 要去掉尾部后缀
            "wxid": re.sub(r"_[0-9a-zA-Z]{4}$", "", n),
            "db_dir": db,
            "db_ready": os.path.isdir(db),
            "mtime": mtime,
        })
    out.sort(key=lambda x: (x["db_ready"], x["mtime"]), reverse=True)
    return out


# ---------------------------------------------------------------- 外部工具

def find_wechat_cli(explicit=""):
    if explicit and os.path.exists(explicit):
        return explicit
    for tpl in CLI_CANDIDATES:
        p = tpl.format(user=_user())
        if os.path.exists(p):
            return os.path.abspath(p)
    # PATH 里找
    from shutil import which
    w = which("wechat-cli") or which("wechat-cli.exe")
    return abspath_or_empty(w)


def find_send_python(explicit=""):
    if explicit and os.path.exists(explicit):
        return explicit
    for tpl in SEND_CANDIDATES:
        p = tpl.format(user=_user())
        if os.path.exists(p):
            return os.path.abspath(p)
    return ""


def abspath_or_empty(p):
    return os.path.abspath(p) if p else ""


def find_decrypt_python(explicit=""):
    """解密图片 / 读语音用的 Python（需要 pycryptodome）。

    优先用 wechat-cli 所在的那个 venv —— 那个环境本来就装了解密依赖。
    """
    if explicit and os.path.exists(explicit):
        return explicit
    cli = find_wechat_cli()
    if cli:
        py = os.path.join(os.path.dirname(cli), "python.exe")
        if os.path.exists(py):
            return py
    return ""


# ---------------------------------------------------------------- 汇总

def detect_all(cfg=None):
    """一次性探测所有环境信息，返回可直接写进 config.json 的字典"""
    cfg = cfg or {}
    reg = read_reg()
    install = reg.get("install_path") or ""
    voip_dir, voip_ver = find_voip_dir(install)
    code, codes = find_account_code()

    roots = find_data_root()
    accounts = []
    data_root = ""
    for r in roots:
        accs = list_accounts(r)
        if accs:
            data_root = r
            accounts = accs
            break
    if not data_root and roots:
        data_root = roots[0]

    # 已在 config 里配过 db_dir 的话，反推它属于哪个根
    known_db = cfg.get("db_dir") or ""
    if known_db and os.path.isdir(known_db):
        parent = os.path.dirname(known_db)
        root = os.path.dirname(parent)
        if root and root not in roots:
            data_root = root

    return {
        "install_path": install,
        "version": reg.get("version") or "",
        "version_name": voip_ver,
        "voip_dir": voip_dir,
        "account_code": code,
        "account_codes": codes,
        "data_root": data_root,
        "data_roots": roots,
        "accounts": accounts,
        "wechat_cli": find_wechat_cli(cfg.get("wechat_cli")),
        "decrypt_python": find_decrypt_python(cfg.get("decrypt_python")),
        "send_python": find_send_python(cfg.get("send_python")),
        "registry": reg.get("raw") or {},
    }


def wechat_running():
    """微信进程在不在跑"""
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="ignore",
                           creationflags=runner.popen_flags())
        if "Weixin.exe" in (r.stdout or ""):
            return True
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WeChat.exe", "/NH"],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="ignore",
                           creationflags=runner.popen_flags())
        return "WeChat.exe" in (r.stdout or "")
    except Exception:
        return False


# ---------------------------------------------------------------- 联系人

def _cli_inproc(args):
    """没有 wechat-cli.exe 时的回落：用打包进来的 wechat_cli 包在本进程里跑。

    exe 版已经把这个包一起打进去了（含 click / pycryptodome / zstandard），
    所以目标机器就算没装 wechat-cli 也能读消息 —— 真正意义上的开箱即用。
    """
    try:
        from click.testing import CliRunner
        from wechat_cli.main import cli as wxcli
    except Exception as e:
        return 1, "", f"内置 wechat-cli 不可用（{type(e).__name__}: {e}）"
    try:
        r = CliRunner().invoke(wxcli, list(args))
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"
    out = r.output or ""
    if r.exit_code != 0:
        extra = repr(r.exception) if r.exception else ""
        return r.exit_code, out, (out + "\n" + extra).strip()
    return 0, out, ""


def run_cli(cli, args, timeout=180):
    """跑 wechat-cli，返回 (rc, stdout, stderr)。

    优先用配置里的 wechat-cli.exe（子进程，最稳）；
    没有就回落到内置的 wechat_cli 包（本进程内跑，免安装）。

    子进程必须强制 UTF-8 —— 否则它向管道输出 JSON 时会用 GBK，
    遇到 ↳ 这种字符直接 UnicodeEncodeError 崩掉（rc=1）。
    """
    if cli and os.path.exists(cli):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            r = subprocess.run([cli, *args], capture_output=True, text=True,
                               encoding="utf-8", errors="ignore", timeout=timeout, env=env,
                               creationflags=runner.popen_flags())
            if r.returncode == 0:
                return 0, r.stdout or "", r.stderr or ""
            # 外部 exe 失败时也试一下内置的，多一条活路
            rc2, out2, err2 = _cli_inproc(args)
            if rc2 == 0:
                return 0, out2, ""
            return r.returncode, r.stdout or out2, (r.stderr or err2 or "")
        except subprocess.TimeoutExpired:
            return 1, "", "调用 wechat-cli 超时"
        except Exception as e:
            return 1, "", f"{type(e).__name__}: {e}"
    return _cli_inproc(args)


def list_contacts(cli, query="", limit=500):
    """列联系人。返回 [{"username","nick_name","remark","display"}]"""
    args = ["contacts", "--limit", str(limit), "--format", "json"]
    if query:
        args += ["--query", query]
    rc, out, err = run_cli(cli, args)
    if rc != 0:
        return [], err.strip()[-500:]
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return [], f"解析失败: {e}; 原始输出: {out[:200]}"
    if isinstance(data, dict):
        data = data.get("contacts") or data.get("data") or []
    return [_norm_contact(c) for c in data if isinstance(c, dict)], ""


def list_sessions(cli, limit=100):
    """最近会话列表（更实用 —— 只会显示真的聊过天的）"""
    rc, out, err = run_cli(cli, ["sessions", "--limit", str(limit), "--format", "json"])
    if rc != 0:
        return [], err.strip()[-500:]
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return [], f"解析失败: {e}; 原始输出: {out[:200]}"
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("data") or []
    return [_norm_contact(c) for c in data if isinstance(c, dict)], ""


def _norm_contact(c):
    """把不同接口的字段名统一"""
    def pick(*keys):
        for k in keys:
            v = c.get(k)
            if v:
                return str(v).strip()
        return ""

    username = pick("username", "wxid", "user_name", "UserName")
    nick = pick("nick_name", "nickname", "nick", "NickName", "display_name")
    remark = pick("remark", "Remark", "remark_name", "alias")
    disp = remark or nick or username
    return {"username": username, "nick_name": nick, "remark": remark, "display": disp}


if __name__ == "__main__":
    # 自测：把所有探测结果打出来（写文件，避免控制台编码问题）
    HERE = app_dir()
    cfg = {}
    cp = os.path.join(HERE, "config.json")
    if os.path.exists(cp):
        try:
            cfg = json.load(open(cp, encoding="utf-8"))
        except Exception:
            pass
    info = detect_all(cfg)
    info["wechat_running"] = wechat_running()
    lines = [json.dumps({k: v for k, v in info.items()
                         if k not in ("registry", "accounts")},
                        ensure_ascii=False, indent=2)]
    lines.append("\n--- 账号 ---")
    for a in info["accounts"]:
        lines.append(json.dumps(a, ensure_ascii=False))
    lines.append("\n--- 注册表 ---")
    lines.append(json.dumps(info["registry"], ensure_ascii=False, indent=2))
    txt = "\n".join(lines)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    out = os.path.join(HERE, "data", "_wxenv.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
