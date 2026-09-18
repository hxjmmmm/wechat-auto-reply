# -*- coding: utf-8 -*-
"""launcher.py — 统一入口（打包成 exe 后的总入口）

同一个程序有三种用法：

    微信自动回复助手.exe                 → 打开图形界面
    微信自动回复助手.exe --daemon-run    → 后台常驻监听（无界面，开机自启用这个）
    微信自动回复助手.exe --script a.py -- <参数...>
                                         → 拿自己当解释器，跑内置的伴随脚本
                                           （发送 / 解密图片 / 读语音），
                                           这样目标机器上不需要装 Python 环境

源码方式跑的时候也一样：python launcher.py / python launcher.py --daemon-run
"""
import os
import runpy
import sys


def _setup():
    """把程序目录塞进 sys.path，保证 import monitor / reply_engine 等能命中"""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [here]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs += [meipass, os.path.join(meipass, "scripts")]
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    for d in reversed(dirs):
        if d and d not in sys.path:
            sys.path.insert(0, d)
    return here


HERE = _setup()

# 无控制台时（pythonw / --noconsole 打包）sys.stdout 是 None，
# 任何在 import 期摸 sys.stdout.buffer 的模块都会当场崩 —— 必须先垫上。
#
# 注意：垫的对象不能直接是 os.devnull。父进程（常驻监听）是用
# subprocess(capture_output=True) 拉起我们的，fd 1/2 其实**是有效的管道**，
# 只是 PyInstaller 的窗口模式没把它接到 sys.stdout 上。
# 所以先试着按 fd 重新打开 —— 这样 send_v2.py 打印的 JSON 才能回传给父进程。
def _attach(fd, fallback_name):
    try:
        return open(fd, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    except OSError:
        try:
            return open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return None


if sys.stdout is None:
    sys.stdout = _attach(1, "stdout")
if sys.stderr is None:
    sys.stderr = _attach(2, "stderr")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_script(name, rest):
    """用当前进程跑一个伴随脚本（等价于 python <name> <rest>）"""
    import runner
    path = runner.script_path(name)
    if not path:
        print(f"找不到脚本: {name}", file=sys.stderr)
        return 2
    sys.argv = [path] + list(rest)
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    runpy.run_path(path, run_name="__main__")
    return 0


def selftest():
    """自检：把界面真的建起来再关掉，验证打包后的运行时（tkinter、脚本定位）都正常。

    打包成 exe 后最怕的就是「双击没反应」——用这个能在命令行里一次看清。
    """
    import traceback
    lines = []
    try:
        import runner
        lines.append("app_dir   = " + runner.app_dir())
        lines.append("frozen    = %s" % runner.is_frozen())
        lines.append("executable= %s" % sys.executable)
        lines.append("meipass   = %s" % getattr(sys, "_MEIPASS", "(无)"))
        for name in ("monitor.py", "send_v2.py", "decrypt_image.py",
                     "voice_text.py", "verify_sent.py"):
            p = runner.script_path(name)
            lines.append("脚本 %-18s %s" % (name, p or "❌ 找不到"))

        import wxenv
        lines.append("wechat-cli  = %s" % (wxenv.find_wechat_cli() or "未找到"))
        lines.append("微信安装目录 = %s" % (wxenv.read_reg().get("install_path") or "未找到"))
        lines.append("数据根目录   = %s" % (wxenv.find_data_root() or "未找到"))

        import reply_engine
        lines.append("提示词       = %d 字（%s）" % (
            len(reply_engine.load_prompt()),
            "自定义" if reply_engine.prompt_is_custom() else "内置默认"))

        # SSL / HTTPS —— 打包最容易踩的坑：
        # libssl-3-x64.dll 抓错版本 → import ssl 直接失败
        # → urllib 没有 HTTPSHandler → "unknown url type: https"
        # → 「测试连接」失败，回复一条也发不出去。所以必须进自检。
        try:
            import ssl as _sslmod
            lines.append("ssl          = OK (%s)" % _sslmod.OPENSSL_VERSION)
        except Exception as e:
            lines.append("ssl          = FAIL %s: %s" % (type(e).__name__, e))
        try:
            import urllib.request as _u
            _u.HTTPSHandler  # noqa: B018 —— 没有 ssl 时这个属性根本不存在
            lines.append("HTTPS        = OK")
        except Exception as e:
            lines.append("HTTPS        = FAIL %s: %s" % (type(e).__name__, e))

        import tkinter
        lines.append("tkinter      = OK (Tk %s)" % tkinter.TkVersion)
        import gui
        app = gui.App()
        app.update_idletasks()
        app.update()
        nb = [w for w in app.winfo_children() if w.winfo_class() == "TNotebook"]
        lines.append("界面         = OK（%d 个页签）" % (nb[0].index("end") if nb else 0))
        app.destroy()
        lines.append("RESULT: PASS")
    except Exception:
        lines.append("RESULT: FAIL")
        lines.append(traceback.format_exc())

    txt = "\n".join(lines)
    print(txt)
    try:
        import runner as _r
        d = os.path.join(_r.app_dir(), "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "selftest.txt"), "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    return 0 if "RESULT: PASS" in txt else 1


def main():
    argv = sys.argv[1:]

    if argv and argv[0] == "--script":
        if len(argv) < 2:
            print("用法: --script <脚本名> [-- 参数...]", file=sys.stderr)
            return 2
        rest = argv[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return run_script(argv[1], rest)

    if argv and argv[0] in ("--daemon-run", "--daemon"):
        import daemon
        sys.argv = ["daemon.py"] + argv[1:]
        return daemon.main()

    if argv and argv[0] in ("--stop-daemon",):
        import daemon
        ok = daemon.request_stop("gui/cli")
        print("已请求停止常驻监听" if ok else "写停止标记失败")
        return 0 if ok else 1

    if argv and argv[0] in ("--version", "-V"):
        print("微信自动回复助手 1.1")
        return 0

    if argv and argv[0] == "--selftest":
        return selftest()

    try:
        import gui
        return gui.main()
    except Exception:
        # 图形界面起不来（比如没装 tkinter）时，至少把原因记下来
        import traceback
        try:
            import runner
            p = os.path.join(runner.app_dir(), "data", "gui_crash.log")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n" + traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main() or 0)
