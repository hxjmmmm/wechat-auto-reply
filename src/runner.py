# -*- coding: utf-8 -*-
"""runner.py — 统一「怎么调一个伴随脚本」

背景：这套工具里有几个必须独立进程跑的脚本（解密图片、读语音、发送消息）。
源码方式运行时它们就是普通 .py，用某个 venv 的 python 跑就行；
但打包成单个 exe 之后，机器上不一定有合适的 python，所以 exe 支持
自己重新拉起自己：

    微信助手.exe --script send_v2.py -- --contact 张三 --message-file reply.txt

这样同一个 exe 既能开界面，也能当解释器跑内置脚本，真正做到"不用装环境"。

对外只暴露两个函数：
    script_path(name)              → 脚本的绝对路径（能找到打包内/源码目录两处）
    cmd_for(cfg, key, name)        → 拼好的命令行前缀 list
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # src/
ROOT = os.path.dirname(HERE)                                # 项目根（src 的上一级）


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def popen_flags():
    """子进程统一加的 creationflags。

    打包成「无窗口 exe」后，每次去调控制台程序（wechat-cli.exe / 各个 venv 的
    python.exe / tasklist）时，Windows 会给它**新建一个控制台窗口** ——
    表现就是屏幕上每隔几秒闪一个黑框。加 CREATE_NO_WINDOW 就不闪了。
    """
    if sys.platform.startswith("win"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def app_dir():
    """程序所在目录 —— 配置、data 都放这。

    打包后必须用 exe 所在目录（不是 _MEIPASS 临时解压目录），
    否则每次运行配置都会丢。

    三种情况：
      1) 父进程通过环境变量 WX_APP_DIR 显式指定 → 直接用。
         这是给「exe 拉起外部 python 跑内置脚本」用的：那种子进程跑的是
         _MEIPASS 里的 .py，它自己算出来的 app_dir 会是 %TEMP% 临时目录，
         于是 config.json / data 全跑到临时目录去了 ——
         表现就是「校验恒失败 FileNotFoundError」，而消息其实早发出去了。
      2) 打包运行 → exe 所在目录
      3) 源码运行 → 项目根（代码在 src/ 下，所以是 src 的上一级；
         靠 config.json / data 这两个标志物确认，万一哪天代码又摊平回根目录也不会找错）
    """
    env_dir = os.environ.get("WX_APP_DIR", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    for cand in (ROOT, HERE):
        if (os.path.exists(os.path.join(cand, "config.json"))
                or os.path.isdir(os.path.join(cand, "data"))):
            return cand
    return HERE


def search_dirs():
    dirs = [app_dir(), HERE]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.append(meipass)
        dirs.append(os.path.join(meipass, "scripts"))
    dirs.append(os.path.join(app_dir(), "scripts"))
    # 截图/OCR 这类辅助脚本放在 tools/ 下，源码运行时也要能按名字找到
    dirs.append(os.path.join(app_dir(), "tools"))
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def script_path(name):
    """在程序目录 / 打包目录里找脚本，返回绝对路径；找不到返回空串"""
    for d in search_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return ""


def cmd_for(cfg, python_key, script_name):
    """拼「跑某个脚本」的命令前缀。

    1) 配置里明确指定了 python（python_key，如 send_python）→ 用它跑源码脚本
    2) 否则若当前是打包 exe → 用 exe 自己重入（--script）
    3) 否则用当前解释器 + 源码路径
    """
    cfg = cfg or {}
    explicit = (cfg.get(python_key) or "").strip()
    src = script_path(script_name)

    if explicit and os.path.exists(explicit) and src:
        return [explicit, "-u", src]
    if is_frozen():
        return [sys.executable, "--script", script_name]
    if src:
        return [sys.executable, "-u", src]
    return []


def child_env():
    """子进程统一 UTF-8 —— 否则 Windows 下会用 GBK 编码管道，遇到 ↳ 这类
    字符直接 UnicodeEncodeError 崩掉（rc=1）。

    同时把 WX_APP_DIR 传下去：子进程（尤其是被外部 python 执行的、位于
    _MEIPASS 的脚本）靠它才能找到真正的 config.json / data 目录，
    不然会去 %TEMP% 里找，一找就 FileNotFoundError。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        env["WX_APP_DIR"] = app_dir()
    except Exception:
        pass
    return env
