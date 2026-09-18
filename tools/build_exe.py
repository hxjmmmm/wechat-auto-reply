# -*- coding: utf-8 -*-
"""build_exe.py — 把「微信自动回复助手」打包成单个 exe

用法（项目目录下）——**必须用打包专用环境跑**，.venv 里没有 PyInstaller：

    python -m venv .build\\venv
    .build\\venv\\Scripts\\python.exe -m pip install -r requirements-build.txt
    .build\\venv\\Scripts\\python.exe tools\\build_exe.py

产物：
    dist\\微信自动回复助手.exe     单文件，双击即用；目标机不需要装 Python
    dist\\config.json             可选：和 exe 放一起，先填好配置（不填也行，界面里改）

打包进去的东西：
    · 图形界面 + 常驻监听 + 回复引擎 + 微信环境探测
    · 发送 / 解密图片 / 读语音 这几个伴随脚本（exe 用 --script 自己调自己）
    · wechat-cli 及其依赖（click / pycryptodome / zstandard）
    · 发送用的 uiautomation / pywin32 / comtypes

也就是说：**目标机器什么都不用装**，只要装了微信并登录过。
"""
import os
import shutil
import subprocess
import sys

# 本文件在 tools/ 下，项目根是它的上一级
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(HERE, "src")
TOOLS_DIR = os.path.join(HERE, "tools")

APP_NAME = "微信自动回复助手"
ENTRY = "launcher.py"

# 会被 exe 以 `--script 名字` 方式重新调起的伴随脚本
SCRIPTS = [
    "monitor.py", "send_v2.py", "decrypt_image.py", "voice_text.py",
    "verify_sent.py", "notify.py", "wxenv.py", "runner.py",
    "reply_engine.py", "daemon.py", "gui.py", "launcher.py",
]
# 注意：**不要**把 README.md / STYLE.md 打进 exe。
# 这两个是开发文档，运行时根本不读（界面里的说明是 gui.py 自带的 SHARE_README），
# 但里面全是本机信息 —— 用户名、联系人昵称、wxid、数据库路径、另一个账号。
# 以前它们就在打包清单里，等于把隐私塞进了每一个发出去的 exe。
EXTRAS = []
# 不是 python 脚本、但运行时同样要按名字找的伴随文件。
# 少了 _shot.ps1 / ocr.ps1，截图 OCR 校验那条路在 exe 里就是空的。
ASSETS = ["_shot.ps1", "ocr.ps1"]

# 整包收进来的第三方库（collect-all：连同数据文件、DLL 一起）
COLLECT_ALL = ["wechat_cli", "Crypto", "uiautomation", "comtypes", "zstandard"]

HIDDEN = [
    "tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.filedialog",
    "tkinter.messagebox", "sqlite3", "winreg", "ctypes",
    "win32api", "win32con", "win32gui", "win32clipboard",
    "wechat_cli", "wechat_cli.main", "wechat_cli.core.context",
    "wechat_cli.core.messages", "wechat_cli.core.crypto",
    # ssl 是被 urllib 在 HTTPSHandler 里「用到才 import」的，静态扫描抓不到，
    # 显式写上，免得某天 ssl 真没进包（那时候报错是 unknown url type: https）。
    "ssl", "_ssl",
]


def sep():
    return ";" if os.name == "nt" else ":"


def exe_path():
    return os.path.join(HERE, "dist", APP_NAME + (".exe" if os.name == "nt" else ""))


def _retire_old_exe():
    """把上一版 exe 挪成 .old（而不是删掉），让 PyInstaller 无需删除旧文件。"""
    exe = exe_path()
    if not os.path.exists(exe):
        return
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.replace(old, old + ".bak")
        os.replace(exe, old)
        print(f"[clean] 旧 exe 已挪到 {os.path.basename(old)}")
    except OSError as e:
        print(f"[warn] 挪不走旧 exe（{e}），打包可能会失败")


def fix_dll_search_path():
    """把「当前解释器自带的 DLL 目录」顶到 PATH 最前面。

    PyInstaller 解析 _ssl.pyd 的依赖时，是按 **PATH 顺序** 找 libssl-3-x64.dll 的。
    本机 PATH 里 `C:\\Program Files\\HP\\HP One Agent` 排在 miniconda 前面，
    于是 HP 自带的 OpenSSL 3.0 被打进了 exe —— 而 _ssl.pyd 是按 OpenSSL 3.5 编的，
    符号对不上，运行期就是：

        ImportError: DLL load failed while importing _ssl: 找不到指定的程序。
        → urllib 没有 HTTPSHandler
        → URLError: <urlopen error unknown url type: https>
        → 「测试连接」失败、回复一条也发不出去

    所以这里先把解释器自己的 Library\\bin / DLLs 顶到最前，保证抓到配套的那份。
    """
    if not os.name == "nt":
        return
    base = getattr(sys, "base_prefix", sys.prefix) or sys.prefix
    mine = [os.path.join(base, "Library", "bin"),
            os.path.join(base, "DLLs"),
            base]
    mine = [d for d in mine if os.path.isdir(d)]
    if not mine:
        return
    rest = [p for p in os.environ.get("PATH", "").split(sep()) if p]
    new = mine + [p for p in rest if os.path.normcase(p) not in
                  {os.path.normcase(m) for m in mine}]
    os.environ["PATH"] = sep().join(new)
    print("[dll] DLL 搜索顺序已前置：" + " | ".join(mine))


def verify(exe):
    """打包完立刻在 exe 里跑一次自检，确认 ssl / HTTPS 真的能用。

    「打包成功」不等于「能联网」——SSL 这种坑只有跑起来才暴露，
    所以把它变成构建流程的一部分，失败就直接报出来。
    """
    print("\n[verify] 在打包后的 exe 里自检 ssl / HTTPS ...")
    try:
        r = subprocess.run([exe, "--selftest"], cwd=os.path.dirname(exe),
                           capture_output=True, timeout=180)
    except Exception as e:
        print(f"[verify] 跑不起来：{e}")
        return False
    txt = ""
    for b in (r.stdout, r.stderr):
        if b:
            txt += b.decode("utf-8", "replace")
    keep = [l for l in txt.splitlines()
            if any(k in l for k in ("ssl", "SSL", "https", "HTTPS", "RESULT"))]
    for l in keep:
        print("   " + l)
    ok = ("RESULT: PASS" in txt) and ("HTTPS        = OK" in txt or "HTTPS: OK" in txt)
    print("[verify] " + ("✅ HTTPS 可用" if ok else "❌ exe 里 HTTPS 不可用，这个包不能用"))
    return ok


def check_env():
    """确认当前解释器装了 PyInstaller。

    踩过的坑：拿运行用的 .venv 直接跑这个脚本，会得到一行没头没尾的
    "No module named PyInstaller" + rc=1。这里把原因和该用什么命令说清楚。
    """
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("当前环境没有 PyInstaller —— 打包要用**打包专用环境**，不是运行用的 .venv：")
        print("    python -m venv .build\\venv")
        print("    .build\\venv\\Scripts\\python.exe -m pip install -r tools\\requirements-build.txt")
        print("    .build\\venv\\Scripts\\python.exe tools\\build_exe.py")
        return False


def build():
    if not check_env():
        return 1
    fix_dll_search_path()
    # PyInstaller 收尾时会 os.remove 掉同名旧 exe。某些环境下「删除」会被安全策略拦住，
    # 整个打包就崩在最后一步 —— 所以先把它挪成 .old（重命名不是删除），让它无旧可删。
    _retire_old_exe()
    args = [sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--onefile",
            "--noconsole",
            "--name", APP_NAME,
            "--distpath", os.path.join(HERE, "dist"),
            "--workpath", os.path.join(HERE, ".build", "work"),
            "--specpath", os.path.join(HERE, ".build"),
            "--paths", SRC_DIR,
            "--paths", HERE,
            "--log-level", "INFO"]

    # 脚本在 src/，ps1 这类辅助文件在 tools/，全部打进 exe 的根（_MEIPASS），
    # 这样 runner.script_path 按文件名就能找到
    for f in SCRIPTS + EXTRAS:
        p = os.path.join(SRC_DIR, f)
        if os.path.exists(p):
            args += ["--add-data", f"{p}{sep()}."]
        else:
            print(f"[warn] 缺少 src/{f}，跳过")
    for f in ASSETS:
        p = os.path.join(TOOLS_DIR, f)
        if os.path.exists(p):
            args += ["--add-data", f"{p}{sep()}."]
        else:
            print(f"[warn] 缺少 tools/{f}，跳过")

    # 让 exe 找到自己（子进程模式）时也能定位到伴随脚本
    args += ["--collect-submodules", "wechat_cli"]

    for m in COLLECT_ALL:
        args += ["--collect-all", m]
    for m in HIDDEN:
        args += ["--hidden-import", m]

    args.append(os.path.join(SRC_DIR, ENTRY))

    print("=" * 70)
    print(f"开始打包：{APP_NAME}.exe")
    print("=" * 70)
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        print(f"\n打包失败，rc={r.returncode}")
        return r.returncode

    exe = exe_path()
    if os.path.exists(exe):
        size = os.path.getsize(exe) / 1024 / 1024
        print(f"\n✅ 打包完成：{exe}  （{size:.1f} MB）")

        # 顺手把「干净版」配置模板放到 dist，方便复制给别人
        tpl = os.path.join(HERE, "dist", "config.示例.json")
        try:
            with open(tpl, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
            print(f"   配置模板：{tpl}")
        except OSError:
            pass

        try:
            verify(exe)
        except Exception as e:
            print(f"[verify] 自检异常：{e}")
    return 0


TEMPLATE = """{
  "contact": "",
  "contact_wxid": "",
  "my_wxid": "",
  "poll_seconds": 20,
  "history_limit": 60,
  "notify": true,
  "auto_send": true,
  "db_dir": "",
  "wechat_cli": "",
  "decrypt_python": "",
  "send_python": "",
  "install_path": "",
  "version_name": "",
  "data_root": "",
  "account_code": "",
  "pet_prefix": "",
  "llm": {
    "enabled": true,
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    "model": "glm-4-flash",
    "vision_model": "glm-4v-flash",
    "temperature": 0.9,
    "max_tokens": 200,
    "max_lines": 3
  }
}
"""


if __name__ == "__main__":
    sys.exit(build())
