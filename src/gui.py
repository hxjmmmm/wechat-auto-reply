# -*- coding: utf-8 -*-
"""gui.py — 微信自动回复助手（图形界面）

五个页签：
    ① 环境 & 账号   自动检测微信（安装目录 / 数据目录 / 账号 / 外部工具），选要监听的账号
    ② 联系人        搜人、选人（支持搜联系人 / 拉最近会话）
    ③ 模型 API      选服务商、填 key、测连通
    ④ 提示词        改人设与语风（写进 prompt.txt）
    ⑤ 运行 & 日志   启停常驻监听、看统计和实时日志

只用标准库（tkinter），不依赖任何第三方包。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

import runner
import wxenv
import monitor
import reply_engine

APP_TITLE = "微信自动回复助手"
APP_VER = "1.1"

# 分享包里的说明文件（给拿到 exe 的人看）
SHARE_README = """微信自动回复助手 · 使用说明
================================

这个 exe 是单文件，不用安装 Python、不用装任何其他东西。
只需要你的电脑上装了**微信 PC 版（4.x），并且当前是登录状态**。

第一次使用，按这 4 步（微信要开着）：

  1. 双击「微信自动回复助手.exe」打开界面
  2. 页签① → 点「自动检测微信」
     （它会自动找到你这台机器上的微信安装目录和数据目录，
       路径和发给你的人那台不一样是正常的，每台机器都不同）
     第一次用会提示「需要提取密钥」，点是，等几秒钟就好
  3. 页签② → 点「最近会话」，双击选中要自动回复的联系人
  4. 页签③ → 选一个服务商，贴入 API Key，点「测试连接」
     推荐「智谱 AI」：open.bigmodel.cn 用手机号注册，文本和看图
     模型都免费（别人给的 key 也能用，直接贴上）

最后：底部「保存全部配置」→「▶ 启动监听」。

想让它开机自己跑：页签⑤ 勾上「开机自动后台运行」。

注意：
  · 它只会回复你在页签② 选中的那一个联系人，其他人不受影响
  · 电脑锁屏的时候它发不出去消息（Windows 安全限制，谁都绕不过），
    会排队等解锁后自动补发；想全自动就把自动锁屏关掉
  · config.json 里存了你的 API Key，别把这份文件再转发给别人
"""

HERE = runner.app_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")
DATA_DIR = os.path.join(HERE, "data")
PROMPT_PATH = os.path.join(HERE, "prompt.txt")
DAEMON_LOG = os.path.join(DATA_DIR, "daemon.log")
DAEMON_LOCK = os.path.join(DATA_DIR, "daemon.lock")
STATS_PATH = os.path.join(DATA_DIR, "daemon_stats.json")
REPLY_PATH = os.path.join(DATA_DIR, "reply.txt")

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
MONO = ("Consolas", 9)

# 常见服务商预设（都是 OpenAI 兼容接口，只填 key 就能用）
PRESETS = {
    "智谱 AI（注册即免费）": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash", "vision_model": "glm-4v-flash"},
    "DeepSeek（便宜）": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "vision_model": ""},
    "通义千问（阿里云）": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-flash", "vision_model": "qwen-vl-plus"},
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "vision_model": "gpt-4o-mini"},
    "自定义（自己填）": {},
}

DEFAULT_CFG = {
    "contact": "", "contact_wxid": "", "my_wxid": "",
    "poll_seconds": 20, "history_limit": 60,
    "notify": True, "auto_send": True,
    "db_dir": "", "wechat_cli": "", "decrypt_python": "", "send_python": "",
    "install_path": "", "version_name": "", "data_root": "", "account_code": "",
    "llm": {"enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "", "model": "glm-4-flash", "vision_model": "glm-4v-flash",
            "temperature": 0.9, "max_tokens": 200, "max_lines": 3},
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            got = json.load(f)
        cfg.update(got or {})
        llm = dict(DEFAULT_CFG["llm"])
        llm.update(got.get("llm") or {})
        cfg["llm"] = llm
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = "%s.%d.tmp" % (CONFIG_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def daemon_alive():
    """常驻监听在不在跑。

    走 monitor.heartbeat_alive（pid 探活优先）—— 以前这里写死「心跳 < 120 秒」，
    而 daemon 一轮最长能跑十几分钟，于是明明在跑却被显示成未运行，
    还能再点一次「启动监听」跑出第二个实例，同一个会话被回两遍。
    """
    return monitor.heartbeat_alive(DAEMON_LOCK)


# ================================================================ 开机自启

def startup_dir():
    if not sys.platform.startswith("win"):
        return ""
    d = os.path.join(os.environ.get("APPDATA") or "", "Microsoft", "Windows",
                     "Start Menu", "Programs", "Startup")
    return d if os.path.isdir(d) else ""


AUTOSTART_NAME = "微信自动回复助手.cmd"
# 上一版（源码方式）留下的启动项，装了新版就顺手清掉，免得跑起两个监听
LEGACY_AUTOSTART = "wechat-auto-reply-daemon.cmd"


def autostart_path():
    d = startup_dir()
    return os.path.join(d, AUTOSTART_NAME) if d else ""


def _legacy_path():
    d = startup_dir()
    return os.path.join(d, LEGACY_AUTOSTART) if d else ""


def autostart_installed():
    p = autostart_path()
    if p and os.path.exists(p):
        return True
    lp = _legacy_path()
    return bool(lp) and os.path.exists(lp)


def _launch_line():
    """拼「后台常驻」的启动命令"""
    if runner.is_frozen():
        return f'start "" "{os.path.abspath(sys.executable)}" --daemon-run'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    py = pyw if os.path.exists(pyw) else sys.executable
    launcher = os.path.join(runner.app_dir(), "launcher.py")
    return f'start "" "{py}" "{launcher}" --daemon-run'


def autostart_install():
    """写一个 .cmd 到「启动」目录。

    注意：cmd.exe 按**系统 ANSI 码页**读批处理，所以含中文路径的文件
    必须写成 GBK 编码，UTF-8 会乱码导致整行跑不通。
    """
    p = autostart_path()
    if not p:
        raise RuntimeError("找不到「启动」目录")
    body = ("@echo off\r\n"
            f"cd /d \"{runner.app_dir()}\"\r\n"
            f"{_launch_line()}\r\n")
    with open(p, "wb") as f:
        f.write(body.encode("gbk", errors="replace"))
    # 清掉旧版（源码方式）的启动项，避免两个监听同时跑
    lp = _legacy_path()
    if lp and os.path.exists(lp):
        try:
            os.remove(lp)
        except OSError:
            pass
    return p


def autostart_uninstall():
    removed = False
    for p in (autostart_path(), _legacy_path()):
        if p and os.path.exists(p):
            try:
                os.remove(p)
                removed = True
            except OSError:
                pass
    return removed


# ================================================================ 主窗口

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VER}")
        self.geometry("980x800")
        self.minsize(880, 700)
        self.cfg = load_cfg()
        self.env = {}
        self.contacts = []
        self._log_seen = 0

        self._init_style()
        self._build()
        self._load_into_ui()
        self.after(300, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(800, self._migrate_autostart)

    def _migrate_autostart(self):
        """旧版（源码方式）的启动项自动升级成新版 exe 的，保持「开机自启」开着。

        只在「旧的还在、新的没有」时动手 —— 也就是升级场景，不动其他情况。
        """
        try:
            new_p, old_p = autostart_path(), _legacy_path()
            if old_p and os.path.exists(old_p) and new_p and not os.path.exists(new_p):
                autostart_install()
                self.var_autostart.set(True)
                self.set_status("已把开机自启升级到新版")
        except Exception:
            pass

    # ------------------------------------------------------------ 样式

    # 配色（微信绿主题）
    C_BG      = "#f2f3f5"   # 窗口底色
    C_CARD    = "#ffffff"   # 卡片 / 输入框 / 文本区
    C_ACCENT  = "#07c160"   # 主色
    C_ACCENT_D = "#05a351"  # 主色 · 悬停/按下
    C_TEXT    = "#1f2329"
    C_MUTED   = "#86909c"
    C_BORDER  = "#dcdfe4"
    C_OK      = "#1a7f37"
    C_BAD     = "#d93026"

    def _init_style(self):
        self.configure(bg=self.C_BG)
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        # 全局基调
        st.configure(".", font=FONT, background=self.C_BG, foreground=self.C_TEXT,
                     bordercolor=self.C_BORDER, darkcolor=self.C_CARD,
                     lightcolor=self.C_CARD, fieldbackground=self.C_CARD,
                     troughcolor=self.C_BG, focuscolor=self.C_ACCENT)
        st.configure("TFrame", background=self.C_BG)
        st.configure("Tab.TFrame", background=self.C_BG, padding=(14, 12))
        st.configure("TLabel", background=self.C_BG, foreground=self.C_TEXT)
        st.configure("TCheckbutton", background=self.C_BG)
        st.map("TCheckbutton", background=[("active", self.C_BG)])

        # 按钮：普通白底描边，主按钮绿色实心
        st.configure("TButton", font=FONT, padding=(12, 6),
                     background=self.C_CARD, foreground=self.C_TEXT,
                     bordercolor=self.C_BORDER, focusthickness=0)
        st.map("TButton",
               background=[("pressed", "#e8eaed"), ("active", "#eff1f3")],
               bordercolor=[("focus", self.C_ACCENT)])
        st.configure("Accent.TButton", font=FONT_B, padding=(16, 7),
                     background=self.C_ACCENT, foreground="#ffffff",
                     bordercolor=self.C_ACCENT)
        st.map("Accent.TButton",
               background=[("pressed", "#048f49"), ("active", self.C_ACCENT_D),
                           ("disabled", "#b7e7cd")],
               bordercolor=[("active", self.C_ACCENT_D)],
               foreground=[("disabled", "#ffffff")])

        # 页签：未选灰、选中白
        st.configure("TNotebook", background=self.C_BG, bordercolor=self.C_BG,
                     tabmargins=(4, 4, 4, 0))
        st.configure("TNotebook.Tab", font=FONT, padding=(20, 9),
                     background="#e4e6ea", foreground="#4e5969")
        st.map("TNotebook.Tab",
               background=[("selected", self.C_CARD), ("active", "#eef0f2")],
               foreground=[("selected", self.C_TEXT)])

        # 分组框
        st.configure("TLabelframe", background=self.C_BG,
                     bordercolor=self.C_BORDER)
        st.configure("TLabelframe.Label", font=FONT_B,
                     background=self.C_BG, foreground="#4e5969")

        # 输入控件：白底、聚焦绿框
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(w, padding=4, fieldbackground=self.C_CARD,
                         bordercolor=self.C_BORDER, arrowcolor="#86909c")
            st.map(w, bordercolor=[("focus", self.C_ACCENT)])

        # 表格
        st.configure("Treeview", font=FONT, rowheight=28,
                     background=self.C_CARD, fieldbackground=self.C_CARD,
                     foreground=self.C_TEXT, bordercolor=self.C_BORDER)
        st.map("Treeview",
               background=[("selected", self.C_ACCENT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", font=FONT_B, padding=(8, 6),
                     background="#f0f1f3", foreground="#4e5969",
                     bordercolor=self.C_BORDER)
        st.map("Treeview.Heading", background=[("active", "#e6e8eb")])

        # 状态文字
        st.configure("Hint.TLabel", font=FONT_S, foreground=self.C_MUTED,
                     background=self.C_BG)
        st.configure("Ok.TLabel", font=FONT_S, foreground=self.C_OK,
                     background=self.C_BG)
        st.configure("Bad.TLabel", font=FONT_S, foreground=self.C_BAD,
                     background=self.C_BG)

    def _style_text(self, w):
        """ScrolledText 是纯 tk 控件，不吃 ttk 样式，这里统一外观"""
        w.configure(bg=self.C_CARD, fg=self.C_TEXT, relief="flat",
                    highlightthickness=1, highlightbackground=self.C_BORDER,
                    highlightcolor=self.C_ACCENT, padx=8, pady=6,
                    insertbackground=self.C_TEXT, selectbackground="#c9f0db")

    # ------------------------------------------------------------ 布局

    def _build(self):
        # 顶部标题栏
        head = tk.Frame(self, bg=self.C_CARD)
        head.pack(fill="x")
        tk.Label(head, text="💬 " + APP_TITLE, bg=self.C_CARD, fg=self.C_TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(
            side="left", padx=(16, 8), pady=12)
        tk.Label(head, text=f"v{APP_VER}", bg=self.C_CARD, fg=self.C_MUTED,
                 font=FONT_S).pack(side="left", pady=12)
        tk.Label(head, text="按 ①→⑤ 顺序配置一遍即可使用",
                 bg=self.C_CARD, fg=self.C_MUTED, font=FONT_S).pack(
            side="right", padx=16, pady=12)
        tk.Frame(self, bg=self.C_BORDER, height=1).pack(fill="x")

        # 底部操作条先 pack（side=bottom），保证窗口偏小时也不被内容挤掉
        tk.Frame(self, bg=self.C_BORDER, height=1).pack(side="bottom", fill="x")
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(bar, text="保存全部配置", style="Accent.TButton",
                   command=self.save_all).pack(side="left")
        self.btn_start = ttk.Button(bar, text="▶ 启动监听", command=self.start_daemon)
        self.btn_start.pack(side="left", padx=(12, 6))
        ttk.Button(bar, text="■ 停止", command=self.stop_daemon).pack(side="left")
        ttk.Button(bar, text="打开项目文件夹", command=self.open_folder).pack(side="left", padx=12)

        self.lbl_status = ttk.Label(bar, text="就绪", style="Hint.TLabel")
        self.lbl_status.pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        self.tab_env = ttk.Frame(nb, style="Tab.TFrame")
        nb.add(self.tab_env, text="① 环境 & 账号")
        self.tab_contact = ttk.Frame(nb, style="Tab.TFrame")
        nb.add(self.tab_contact, text="② 联系人")
        self.tab_llm = ttk.Frame(nb, style="Tab.TFrame")
        nb.add(self.tab_llm, text="③ 模型 API")
        self.tab_prompt = ttk.Frame(nb, style="Tab.TFrame")
        nb.add(self.tab_prompt, text="④ 提示词")
        self.tab_run = ttk.Frame(nb, style="Tab.TFrame")
        nb.add(self.tab_run, text="⑤ 运行 & 日志")

        self._build_env()
        self._build_contact()
        self._build_llm()
        self._build_prompt()
        self._build_run()

    # ---------- ① 环境 & 账号 ----------

    def _build_env(self):
        f = self.tab_env

        box = ttk.LabelFrame(f, text="微信环境（点「自动检测」自动填）")
        box.pack(fill="x", padx=4, pady=6)
        self.env_labels = {}
        rows = [
            ("wechat", "微信进程"), ("install", "安装目录"), ("version", "版本"),
            ("data_root", "数据根目录"), ("code", "账号 code（解密用）"),
            ("cli", "wechat-cli"), ("send", "发送环境"), ("decrypt", "解密环境"),
        ]
        for i, (key, label) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(box, text=label + "：").grid(row=r, column=c * 2, sticky="e", padx=(10, 4), pady=3)
            v = ttk.Label(box, text="—", style="Hint.TLabel", anchor="w")
            v.grid(row=r, column=c * 2 + 1, sticky="we", padx=(0, 14), pady=3)
            box.columnconfigure(c * 2 + 1, weight=1)
            self.env_labels[key] = v

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=4)
        ttk.Button(bar, text="自动检测微信", style="Accent.TButton",
                   command=self.detect_env).pack(side="left")
        ttk.Button(bar, text="重新提取密钥", command=self.reinit_keys).pack(side="left", padx=6)
        ttk.Button(bar, text="生成分享包", command=self.make_share_package).pack(side="left")
        ttk.Label(bar, text="（发给别人的话点「生成分享包」，对方什么都不用装）",
                  style="Hint.TLabel").pack(side="left", padx=8)

        acc = ttk.LabelFrame(f, text="要监听的微信账号")
        acc.pack(fill="x", padx=4, pady=10)
        ttk.Label(acc, text="账号：").grid(row=0, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_account = tk.StringVar()
        self.cmb_account = ttk.Combobox(acc, textvariable=self.var_account,
                                        state="readonly", width=46, font=FONT)
        self.cmb_account.grid(row=0, column=1, sticky="we", pady=6)
        self.cmb_account.bind("<<ComboboxSelected>>", lambda e: self._on_account_pick())
        ttk.Label(acc, text="数据库目录：").grid(row=1, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_dbdir = tk.StringVar()
        ttk.Entry(acc, textvariable=self.var_dbdir, font=FONT).grid(
            row=1, column=1, sticky="we", pady=6, padx=(0, 10))
        ttk.Label(acc, text="我自己的 wxid：").grid(row=2, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_mywxid = tk.StringVar()
        ttk.Entry(acc, textvariable=self.var_mywxid, font=FONT).grid(
            row=2, column=1, sticky="we", pady=6, padx=(0, 10))
        ttk.Label(acc, text="（用来区分「她说的」和「我说的」，自动检测会填）",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=2, sticky="w",
                                            padx=12, pady=(0, 8))
        acc.columnconfigure(1, weight=1)

        adv = ttk.LabelFrame(f, text="外部工具路径（一般不用改，检测不到时手动指定）")
        adv.pack(fill="x", padx=4, pady=(0, 8))
        self.path_vars = {}
        for i, (key, label) in enumerate([("wechat_cli", "wechat-cli"),
                                          ("decrypt_python", "解密/语音 Python"),
                                          ("send_python", "发送 Python")]):
            ttk.Label(adv, text=label + "：").grid(row=i, column=0, sticky="e", padx=(10, 4), pady=3)
            v = tk.StringVar()
            self.path_vars[key] = v
            ttk.Entry(adv, textvariable=v, font=FONT_S).grid(row=i, column=1, sticky="we", pady=3)
            ttk.Button(adv, text="浏览…", width=7,
                       command=lambda k=key: self._browse(k)).grid(row=i, column=2, padx=(6, 10), pady=3)
        adv.columnconfigure(1, weight=1)

    def _browse(self, key):
        title = {"wechat_cli": "选择 wechat-cli.exe",
                 "decrypt_python": "选择 python.exe（解密/语音用）",
                 "send_python": "选择 python.exe（发送用）"}[key]
        p = filedialog.askopenfilename(title=title, filetypes=[("可执行文件", "*.exe"), ("全部", "*.*")])
        if p:
            self.path_vars[key].set(p)

    def detect_env(self):
        def job():
            return wxenv.detect_all(self.cfg)

        def done(info):
            self.env = info
            self._render_env(info)
            # 把探测结果落进配置，下次打开不用重测
            for k in ("install_path", "version_name", "data_root", "account_code",
                      "wechat_cli", "decrypt_python", "send_python"):
                if info.get(k):
                    self.cfg[k] = info[k]
            if info.get("account_codes"):
                self.cfg["account_code"] = info["account_codes"][0]
            # 外部工具路径：本机没找到、且旧值在这台机器上根本不存在 → 清掉
            # （比如这份配置是从别的机器上复制来的，里面的绝对路径在这台机上是死的；
            #  留空才会走 exe 内置的 wechat-cli / Crypto / uiautomation）
            for k in ("wechat_cli", "decrypt_python", "send_python"):
                cur = (self.cfg.get(k) or "").strip()
                if cur and not os.path.exists(cur):
                    self.cfg[k] = ""
                    if k in self.path_vars:
                        self.path_vars[k].set("")
            # 同步三个路径输入框显示探测结果
            for k in ("wechat_cli", "decrypt_python", "send_python"):
                if info.get(k) and k in self.path_vars:
                    self.path_vars[k].set(info[k])
            # 账号下拉
            items = []
            for a in info.get("accounts") or []:
                tag = "" if a["db_ready"] else "（数据不完整）"
                items.append(f"{a['dir_name']}{tag}")
            self.cmb_account["values"] = items
            if items and not self.var_account.get():
                self.cmb_account.current(0)
                self._on_account_pick()
            self.save_all(silent=True)
            self.set_status("环境检测完成")
            # 试读一次会话：第一次用的新机器往往还没提取密钥，读不了就引导用户点一下
            self._probe_readable()

        self.run_bg(job, done, busy="正在检测微信环境…")

    def _probe_readable(self):
        """检测完试读一次，确认真的能读到消息（新机器第一次需要提取密钥）"""
        cli = self.path_vars["wechat_cli"].get() or self.cfg.get("wechat_cli")

        def job():
            return wxenv.run_cli(cli, ["sessions", "--limit", "1", "--format", "json"],
                                 timeout=120)

        def done(res):
            rc, out, err = res
            if rc == 0:
                self.set_status("环境检测完成，可以正常读取消息")
                return
            # 读不了 —— 多半是第一次用，还没提取过密钥
            if messagebox.askyesno(
                    APP_TITLE,
                    "环境检测到了，但还读不到消息。\n\n"
                    "第一次使用需要先从微信里提取一次密钥（几秒钟，只读操作）。\n"
                    "现在执行吗？"):
                self.reinit_keys()
            else:
                self.set_status("提示：点「重新提取密钥」后才能读取消息")

        self.run_bg(job, done, busy="正在验证能否读取消息…")

    def _render_env(self, info):
        ok, bad = "Ok.TLabel", "Bad.TLabel"

        def put(key, text, good=True):
            lbl = self.env_labels[key]
            lbl.configure(text=text or "—")
            try:
                lbl.configure(style=ok if good else bad)
            except tk.TclError:
                pass

        running = wxenv.wechat_running()
        put("wechat", "● 正在运行" if running else "○ 未运行（读取仍可用，发送需启动微信）", running)
        put("install", info.get("install_path") or "未找到", bool(info.get("install_path")))
        put("version", info.get("version_name") or info.get("version") or "未知",
            bool(info.get("version_name")))
        put("data_root", info.get("data_root") or "未找到", bool(info.get("data_root")))
        put("code", info.get("account_code") or "未找到", bool(info.get("account_code")))
        put("cli", info.get("wechat_cli") or "未找到（将使用内置 wechat-cli）",
            bool(info.get("wechat_cli")))
        put("send", info.get("send_python") or "未找到（将用本程序内置环境）", True)
        put("decrypt", info.get("decrypt_python") or "未找到", bool(info.get("decrypt_python")))

        for k, v in self.path_vars.items():
            if info.get(k) and not v.get():
                v.set(info[k])
        if info.get("data_root") and not self.var_dbdir.get():
            accs = info.get("accounts") or []
            if accs:
                self.var_dbdir.set(accs[0]["db_dir"])
                self.var_mywxid.set(accs[0]["wxid"])

    def _on_account_pick(self):
        idx = self.cmb_account.current()
        accs = self.env.get("accounts") or []
        if 0 <= idx < len(accs):
            self.var_dbdir.set(accs[idx]["db_dir"])
            self.var_mywxid.set(accs[idx]["wxid"])

    def make_share_package(self):
        """生成一个可以直接发给别人的分享包（exe + 干净配置 + 使用说明）。

        关键：配置必须是**干净模板** —— 不能带自己的 API Key、联系人、
        本机绝对路径。对方打开后点「自动检测微信」会自动填他自己机器的路径。
        """
        if not messagebox.askyesno(
                APP_TITLE,
                "生成一个可以直接发给别人的分享包？\n\n"
                "里面包含：\n"
                "  · 微信自动回复助手.exe（单文件，对方不用装 Python）\n"
                "  · 干净的 config.json（**不含**你的 key、联系人和本机路径）\n"
                "  · 使用说明.txt"):
            return

        def job():
            outdir = os.path.join(HERE, "分享包")
            os.makedirs(outdir, exist_ok=True)
            exe_src = (os.path.abspath(sys.executable) if runner.is_frozen()
                       else os.path.join(HERE, "dist", "微信自动回复助手.exe"))
            if not os.path.exists(exe_src):
                raise RuntimeError("找不到 exe。源码运行时请先用 build_exe.py 打包一次。")
            shutil.copy2(exe_src, os.path.join(outdir, "微信自动回复助手.exe"))

            # 干净配置：不带 key、不带联系人、不带任何本机路径
            clean = dict(DEFAULT_CFG)
            clean["llm"] = dict(DEFAULT_CFG["llm"])
            with open(os.path.join(outdir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)

            with open(os.path.join(outdir, "使用说明.txt"), "w", encoding="utf-8") as f:
                f.write(SHARE_README)
            return outdir

        def done(outdir):
            if messagebox.askyesno(APP_TITLE, f"分享包已生成：\n{outdir}\n\n打开文件夹看看？"):
                self._open_path(outdir)

        self.run_bg(job, done, busy="正在生成分享包…")

    def _open_path(self, p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)             # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))

    def reinit_keys(self):
        cli = self.path_vars["wechat_cli"].get() or self.cfg.get("wechat_cli")
        db = self.var_dbdir.get() or self.cfg.get("db_dir")
        if not db:
            messagebox.showwarning(APP_TITLE, "先点「自动检测微信」，把数据库目录填上")
            return

        def job():
            rc, out, err = wxenv.run_cli(cli, ["init", "--force", "--db-dir", db], timeout=600)
            return rc, (out or "")[-400:], (err or "")[-400:]

        def done(res):
            rc, out, err = res
            if rc == 0:
                messagebox.showinfo(APP_TITLE, "密钥重新提取成功。\n下次检测即可读到最新消息。")
            else:
                messagebox.showerror(APP_TITLE, f"提取失败（rc={rc}）\n{err or out}")

        self.run_bg(job, done, busy="正在重新提取密钥…")

    # ---------- ② 联系人 ----------

    def _build_contact(self):
        f = self.tab_contact
        top = ttk.Frame(f)
        top.pack(fill="x", padx=4, pady=(8, 4))
        ttk.Label(top, text="搜索：").pack(side="left")
        self.var_search = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.var_search, font=FONT, width=28)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.load_contacts(self.var_search.get()))
        ttk.Button(top, text="搜索联系人",
                   command=lambda: self.load_contacts(self.var_search.get())).pack(side="left", padx=4)
        ttk.Button(top, text="最近会话", command=self.load_sessions).pack(side="left")
        ttk.Button(top, text="全部联系人", command=lambda: self.load_contacts("")).pack(side="left", padx=4)

        cols = ("display", "nick", "wxid")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c, t, w in [("display", "显示名（备注优先）", 220), ("nick", "昵称", 220), ("wxid", "wxid", 300)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_contact_pick())
        self.tree.bind("<Double-1>", lambda e: self._on_contact_pick())

        sel = ttk.LabelFrame(f, text="已选中的回复对象")
        sel.pack(fill="x", padx=4, pady=(4, 8))
        self.lbl_contact = ttk.Label(sel, text="（还没选）", font=FONT_B)
        self.lbl_contact.pack(anchor="w", padx=12, pady=8)

        ttk.Label(f, text="提示：群聊也能选，但「朋友」优先——选好后会自动填进配置里的 contact / contact_wxid。",
                  style="Hint.TLabel").pack(anchor="w", padx=8, pady=(0, 8))

    def load_contacts(self, query=""):
        cli = self.path_vars["wechat_cli"].get() or self.cfg.get("wechat_cli")

        def job():
            return wxenv.list_contacts(cli, query, limit=800)

        def done(res):
            items, err = res
            if err:
                messagebox.showerror(APP_TITLE, f"读取联系人失败：\n{err}")
                return
            self._fill_tree(items)
            self.set_status(f"载入 {len(items)} 个联系人")

        self.run_bg(job, done, busy="正在读取联系人…")

    def load_sessions(self):
        cli = self.path_vars["wechat_cli"].get() or self.cfg.get("wechat_cli")

        def job():
            return wxenv.list_sessions(cli, limit=120)

        def done(res):
            items, err = res
            if err:
                messagebox.showerror(APP_TITLE, f"读取会话失败：\n{err}")
                return
            self._fill_tree(items)
            self.set_status(f"载入 {len(items)} 个最近会话")

        self.run_bg(job, done, busy="正在读取最近会话…")

    def _fill_tree(self, items):
        self.tree.delete(*self.tree.get_children())
        self.contacts = items
        for c in items:
            self.tree.insert("", "end", values=(c["display"], c["nick_name"], c["username"]))

    def _on_contact_pick(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.contacts):
            return
        c = self.contacts[idx]
        self.lbl_contact.configure(
            text=f"{c['display']}   （昵称：{c['nick_name'] or '—'}   wxid：{c['username']}）")
        # 搜索导航用的是显示名，所以 contact 存显示名
        self.cfg["contact"] = c["display"]
        self.cfg["contact_wxid"] = c["username"]
        self.set_status(f"已选：{c['display']}")

    # ---------- ③ 模型 ----------

    def _build_llm(self):
        f = self.tab_llm
        box = ttk.LabelFrame(f, text="服务商")
        box.pack(fill="x", padx=4, pady=8)
        ttk.Label(box, text="预设：").grid(row=0, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_preset = tk.StringVar(value=list(PRESETS)[0])
        cmb = ttk.Combobox(box, textvariable=self.var_preset, state="readonly",
                           values=list(PRESETS), width=32, font=FONT)
        cmb.grid(row=0, column=1, sticky="w", pady=6)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())
        ttk.Label(box, text="选预设会自动填好下面三个字段，只需要再贴一个 API Key",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

        form = ttk.LabelFrame(f, text="接口参数")
        form.pack(fill="x", padx=4, pady=4)
        self.llm_vars = {}
        fields = [("base_url", "接口地址", 62), ("model", "文本模型", 30),
                  ("vision_model", "看图模型（可留空）", 30)]
        r = 0
        for key, label, w in fields:
            ttk.Label(form, text=label + "：").grid(row=r, column=0, sticky="e", padx=(10, 4), pady=5)
            v = tk.StringVar()
            self.llm_vars[key] = v
            ttk.Entry(form, textvariable=v, font=FONT, width=w).grid(
                row=r, column=1, sticky="we", pady=5, padx=(0, 10))
            r += 1

        ttk.Label(form, text="API Key：").grid(row=r, column=0, sticky="e", padx=(10, 4), pady=5)
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(form, textvariable=self.var_key, font=FONT, show="●")
        self.ent_key.grid(row=r, column=1, sticky="we", pady=5, padx=(0, 10))
        self.var_showkey = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示", variable=self.var_showkey,
                        command=self._toggle_key).grid(row=r, column=2, padx=(0, 10))
        r += 1

        ttk.Label(form, text="创造力：").grid(row=r, column=0, sticky="e", padx=(10, 4), pady=5)
        self.var_temp = tk.StringVar()
        ttk.Spinbox(form, textvariable=self.var_temp, from_=0.0, to=1.0, increment=0.1,
                    width=8, font=FONT).grid(row=r, column=1, sticky="w", pady=5)
        ttk.Label(form, text="（0～1，越大越随性；聊天建议 0.8～1.0）",
                  style="Hint.TLabel").grid(row=r, column=2, sticky="w", padx=(0, 10))
        r += 1
        ttk.Label(form, text="单次上限：").grid(row=r, column=0, sticky="e", padx=(10, 4), pady=5)
        self.var_maxtok = tk.StringVar()
        ttk.Spinbox(form, textvariable=self.var_maxtok, from_=50, to=2000, increment=50,
                    width=8, font=FONT).grid(row=r, column=1, sticky="w", pady=5)
        ttk.Label(form, text="最多发几条：").grid(row=r, column=2, sticky="e", padx=(10, 4))
        self.var_maxlines = tk.StringVar()
        ttk.Spinbox(form, textvariable=self.var_maxlines, from_=1, to=6, increment=1,
                    width=6, font=FONT).grid(row=r, column=3, sticky="w", padx=(0, 10))
        form.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=4, pady=8)
        ttk.Button(bar, text="测试连接", style="Accent.TButton",
                   command=self.test_llm).pack(side="left")
        ttk.Label(bar, text="用一句假消息试生成，确认 key 和模型都通",
                  style="Hint.TLabel").pack(side="left", padx=8)

        self.txt_llm = ScrolledText(f, height=10, font=FONT, wrap="word")
        self.txt_llm.pack(fill="both", expand=True, padx=4, pady=(0, 8))

    def _toggle_key(self):
        self.ent_key.configure(show="" if self.var_showkey.get() else "●")

    def _apply_preset(self):
        p = PRESETS.get(self.var_preset.get()) or {}
        for k, v in p.items():
            if k in self.llm_vars:
                self.llm_vars[k].set(v)

    def _collect_llm(self):
        try:
            temp = float(self.var_temp.get() or 0.9)
        except ValueError:
            temp = 0.9
        try:
            maxtok = int(float(self.var_maxtok.get() or 200))
        except ValueError:
            maxtok = 200
        try:
            maxlines = int(float(self.var_maxlines.get() or 3))
        except ValueError:
            maxlines = 3
        return {
            "enabled": True,
            "base_url": self.llm_vars["base_url"].get().strip(),
            "api_key": self.var_key.get().strip(),
            "model": self.llm_vars["model"].get().strip(),
            "vision_model": self.llm_vars["vision_model"].get().strip(),
            "temperature": max(0.0, min(1.0, temp)),
            "max_tokens": maxtok,
            "max_lines": maxlines,
        }

    def test_llm(self):
        llm = self._collect_llm()
        if not llm["api_key"]:
            messagebox.showwarning(APP_TITLE, "先填 API Key")
            return
        self.save_all(silent=True)
        demo = {
            "context": [{"from_her": True, "text": "今天好累"},
                        {"from_her": False, "text": "咋了"}],
            "new_messages": [{"text": "被领导骂了 烦死了"}],
            "new_images": [],
        }

        def job():
            cfg = dict(self.cfg)
            cfg["llm"] = llm
            lines, meta = reply_engine.generate(cfg, demo)
            return lines, meta

        def done(res):
            lines, meta = res
            t = [f"✅ 模型：{meta.get('model')}",
                 f"   用量：{meta.get('usage')}",
                 "",
                 "生成的回复（实际会这么发）：",
                 "─" * 40]
            t += ["  " + x for x in lines]
            t += ["─" * 40, "", "原始输出：", meta.get("raw", "")]
            self._set_text(self.txt_llm, "\n".join(t))
            self.set_status("模型测试通过")

        self.run_bg(job, done, busy="正在测试模型…")

    # ---------- ④ 提示词 ----------

    def _build_prompt(self):
        f = self.tab_prompt
        ttk.Label(f, text="这段文字就是给模型的「人设 + 语风」说明。改完点保存即生效，"
                          "不需要重启。", style="Hint.TLabel").pack(anchor="w", padx=8, pady=(8, 4))
        self.txt_prompt = ScrolledText(f, font=FONT, wrap="word")
        self.txt_prompt.pack(fill="both", expand=True, padx=8, pady=4)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=8, pady=(0, 10))
        ttk.Button(bar, text="保存提示词", style="Accent.TButton",
                   command=self.save_prompt).pack(side="left")
        ttk.Button(bar, text="恢复默认", command=self.reset_prompt).pack(side="left", padx=6)
        ttk.Button(bar, text="预览实际发送内容", command=self.preview_prompt).pack(side="left")
        self.lbl_prompt = ttk.Label(bar, text="", style="Hint.TLabel")
        self.lbl_prompt.pack(side="right")

    def save_prompt(self):
        txt = self.txt_prompt.get("1.0", "end").rstrip()
        if not txt.strip():
            messagebox.showwarning(APP_TITLE, "提示词不能为空")
            return
        reply_engine.save_prompt(txt)
        self.save_all(silent=True)
        self._refresh_prompt_state()
        self.set_status("提示词已保存")

    def reset_prompt(self):
        if not messagebox.askyesno(APP_TITLE, "恢复成内置的默认提示词？当前改动会丢失。"):
            return
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", reply_engine.DEFAULT_PROMPT)
        self.set_status("已填入默认提示词，记得点「保存提示词」")

    def _refresh_prompt_state(self):
        if reply_engine.prompt_is_custom():
            self.lbl_prompt.configure(text="当前：自定义提示词", style="Ok.TLabel")
        else:
            self.lbl_prompt.configure(text="当前：内置默认", style="Hint.TLabel")

    def preview_prompt(self):
        demo = {"context": [{"from_her": True, "text": "今天好累"},
                            {"from_her": False, "text": "咋了"}],
                "new_messages": [{"text": "面试又被拒了 烦死了"}],
                "new_images": []}
        content, _ = reply_engine.build_user_content(demo, False)
        win = tk.Toplevel(self)
        win.title("实际发送给模型的内容")
        win.geometry("760x560")
        txt = ScrolledText(win, font=MONO, wrap="word")
        self._style_text(txt)
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", "===== 系统提示词 =====\n" + reply_engine.load_prompt()
                   + "\n\n===== 用户消息 =====\n" + str(content))
        txt.configure(state="disabled")

    # ---------- ⑤ 运行 & 日志 ----------

    def _build_run(self):
        f = self.tab_run

        top = ttk.LabelFrame(f, text="开关")
        top.pack(fill="x", padx=4, pady=8)
        self.var_autosend = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动发送（关掉＝只生成草稿，不操作微信）",
                        variable=self.var_autosend).grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self.var_notify = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="有动作时弹桌面通知",
                        variable=self.var_notify).grid(row=1, column=0, sticky="w", padx=12, pady=6)
        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="开机自动后台运行（登录后无需再打开本窗口）",
                        variable=self.var_autostart,
                        command=self._toggle_autostart).grid(row=2, column=0, sticky="w", padx=12, pady=6)
        ttk.Label(top, text="轮询间隔（秒）：").grid(row=3, column=0, sticky="w", padx=12, pady=6)
        self.var_poll = tk.StringVar()
        ttk.Spinbox(top, textvariable=self.var_poll, from_=5, to=600, increment=5,
                    width=8, font=FONT).grid(row=3, column=1, sticky="w")

        st = ttk.LabelFrame(f, text="运行状态")
        st.pack(fill="x", padx=4, pady=4)
        self.lbl_run = ttk.Label(st, text="未运行", font=FONT_B)
        self.lbl_run.grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 4))
        self.lbl_stats = ttk.Label(st, text="—", style="Hint.TLabel")
        self.lbl_stats.grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 10))

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Button(bar, text="查看最新草稿", command=self.show_draft).pack(side="left")
        ttk.Button(bar, text="重建基线", command=self.rebuild_baseline).pack(side="left", padx=6)
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="left")
        ttk.Button(bar, text="打开项目文件夹", command=self.open_folder).pack(side="left", padx=6)
        ttk.Label(f, text="「重建基线」＝把当前聊天记录全部当作已读，之后只回复新消息"
                          "（第一次装好时程序会自动做一次）。",
                  style="Hint.TLabel").pack(anchor="w", padx=8, pady=(0, 4))

        ttk.Label(f, text="实时日志（daemon.log）：", style="Hint.TLabel").pack(anchor="w", padx=8, pady=(6, 2))
        self.txt_log = ScrolledText(f, font=MONO, wrap="none", height=14)
        self._style_text(self.txt_log)
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        self.txt_log.configure(state="disabled")

    def _toggle_autostart(self):
        try:
            if self.var_autostart.get():
                p = autostart_install()
                self.set_status(f"已设置开机自启：{p}")
            else:
                autostart_uninstall()
                self.set_status("已取消开机自启")
        except Exception as e:
            self.var_autostart.set(not self.var_autostart.get())
            messagebox.showerror(APP_TITLE, f"设置开机自启失败：{e}")

    def show_draft(self):
        if not os.path.exists(REPLY_PATH):
            messagebox.showinfo(APP_TITLE, "还没有草稿（data/reply.txt 不存在）")
            return
        try:
            with open(REPLY_PATH, encoding="utf-8") as f:
                txt = f.read()
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        messagebox.showinfo("最新草稿", txt.strip() or "（空）")

    def rebuild_baseline(self):
        # 常驻监听还在跑的时候不能直接重建：两边会同时读写 data/state.json，
        # 而且重建基线会清空待回复队列，正在排队待发的消息会被无声丢掉。
        if daemon_alive():
            if not messagebox.askyesno(
                    APP_TITLE,
                    "监听正在运行。\n\n重建基线需要先把它停下来 —— 否则会和它抢写 state.json，"
                    "排队待发的消息也可能被清掉。\n\n现在停止监听并重建吗？"
                    "（重建完记得再点一次「▶ 启动监听」）"):
                return
            self.set_status("正在停止监听…")
            if not self._stop_daemon_and_wait():
                messagebox.showwarning(
                    APP_TITLE, "监听没能及时退出（它可能正卡在一轮发送里）。\n"
                               "请稍等一会儿看到状态变成「未运行」后，再点「重建基线」。")
                return

        if not messagebox.askyesno(APP_TITLE,
                                   "把当前聊天记录全部视为已读？\n\n"
                                   "之后只回复新消息，历史消息不会被回复。"):
            return
        self.save_all(silent=True)

        def job():
            import monitor
            ns = type("NS", (), {})()
            ns.clear_pending = False
            ns.init = True
            ns.context = 0
            ns.out = ""
            ns.json = False
            ns.text = False
            ns._quiet = True
            code, res = monitor.run_once(ns)
            return code, (res or {})

        def done(r):
            code, res = r
            if code == 0:
                messagebox.showinfo(APP_TITLE,
                                    f"已重建基线：{res.get('baseline_count', 0)} 条消息视为已读。\n"
                                    f"接下来只回复新消息。")
            else:
                messagebox.showerror(APP_TITLE, "重建失败，详见 data/monitor.log")

        self.run_bg(job, done, busy="正在重建基线…")

    def clear_log(self):
        try:
            if os.path.exists(DAEMON_LOG):
                os.remove(DAEMON_LOG)
            self._set_text(self.txt_log, "")
            self._log_seen = 0
            self.set_status("日志已清空")
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def open_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(HERE)          # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", HERE])
            else:
                subprocess.Popen(["xdg-open", HERE])
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))

    # ------------------------------------------------------------ 启停

    def start_daemon(self):
        self.save_all(silent=True)
        if self.cfg.get("auto_send", True) and not (self.cfg.get("contact") or "").strip():
            messagebox.showwarning(APP_TITLE, "还没选联系人，去「② 联系人」选一个")
            return
        llm = self.cfg.get("llm") or {}
        if not (llm.get("api_key") or "").strip():
            messagebox.showwarning(APP_TITLE, "还没填 API Key，去「③ 模型 API」填一个")
            return
        if daemon_alive():
            messagebox.showinfo(APP_TITLE, "监听已经在运行了")
            return

        try:
            os.remove(os.path.join(DATA_DIR, "stop.flag"))
        except OSError:
            pass

        if runner.is_frozen():
            cmd = [sys.executable, "--daemon-run"]
        else:
            # 代码在 src/ 下，统一走 runner 按名字定位（打包/源码两种布局都能找到）
            cmd = [sys.executable, runner.script_path("daemon.py") or
                   os.path.join(HERE, "src", "daemon.py")]
        flags = 0
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(os.path.join(DATA_DIR, "daemon_start.log"), "ab") as lf:
                p = subprocess.Popen(cmd, cwd=HERE, creationflags=flags,
                                     stdout=lf, stderr=lf, stdin=subprocess.DEVNULL)
            self.set_status(f"已启动监听（pid {p.pid}）")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"启动失败：{e}")

    def _write_stop_flag(self):
        try:
            with open(os.path.join(DATA_DIR, "stop.flag"), "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat(timespec="seconds") + "\n")
            return True
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"写停止标记失败：{e}")
            return False

    def stop_daemon(self):
        if self._write_stop_flag():
            self.set_status("已请求停止（要等当前这一轮跑完，最长可能几分钟）")

    def _stop_daemon_and_wait(self, timeout=30):
        """写停止标记并等常驻进程真的退出，返回是否等到"""
        if not self._write_stop_flag():
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.update()
            if not daemon_alive():
                return True
            time.sleep(0.5)
        return not daemon_alive()

    # ------------------------------------------------------------ 读写配置

    def _load_into_ui(self):
        c = self.cfg
        self.var_dbdir.set(c.get("db_dir") or "")
        self.var_mywxid.set(c.get("my_wxid") or "")
        for k, v in self.path_vars.items():
            v.set(c.get(k) or "")
        llm = c.get("llm") or {}
        for k in self.llm_vars:
            self.llm_vars[k].set(llm.get(k) or "")
        self.var_key.set(llm.get("api_key") or "")
        self.var_temp.set(str(llm.get("temperature", 0.9)))
        self.var_maxtok.set(str(llm.get("max_tokens", 200)))
        self.var_maxlines.set(str(llm.get("max_lines", 3)))
        self.var_autosend.set(bool(c.get("auto_send", True)))
        self.var_notify.set(bool(c.get("notify", True)))
        self.var_poll.set(str(c.get("poll_seconds", 20)))
        self.var_autostart.set(autostart_installed())

        if c.get("contact"):
            self.lbl_contact.configure(text=f"{c['contact']}   （wxid：{c.get('contact_wxid') or '—'}）")

        # 提示词
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", reply_engine.load_prompt())
        self._refresh_prompt_state()

        # 环境（用已存的路径先渲染一遍，避免一片空白）
        env = {k: c.get(k) for k in ("install_path", "version_name", "data_root",
                                     "account_code", "wechat_cli",
                                     "decrypt_python", "send_python")}
        self._render_env(env)

    def _collect(self):
        c = dict(self.cfg) if self.cfg else dict(DEFAULT_CFG)
        c["db_dir"] = self.var_dbdir.get().strip()
        c["my_wxid"] = self.var_mywxid.get().strip()
        for k, v in self.path_vars.items():
            c[k] = v.get().strip()
        c["auto_send"] = bool(self.var_autosend.get())
        c["notify"] = bool(self.var_notify.get())
        try:
            c["poll_seconds"] = max(5, int(float(self.var_poll.get() or 20)))
        except ValueError:
            c["poll_seconds"] = 20
        c["llm"] = self._collect_llm()
        return c

    def save_all(self, silent=False):
        self.cfg = self._collect()
        try:
            save_cfg(self.cfg)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"保存失败：{e}")
            return
        if not silent:
            self.set_status("配置已保存到 config.json")

    def _on_close(self):
        try:
            self.save_all(silent=True)
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------ 通用

    def set_status(self, text):
        self.lbl_status.configure(text=text)

    def run_bg(self, fn, done=None, busy=None):
        """后台线程跑耗时活（调 wechat-cli / 调模型），别把界面卡住"""
        if busy:
            self.set_status(busy)
        self.configure(cursor="watch")

        def worker():
            try:
                res, err = fn(), None
            except Exception as e:                       # noqa: BLE001
                res, err = None, e
            self.after(0, lambda: self._finish(done, res, err))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, done, res, err):
        self.configure(cursor="")
        if err is not None:
            self.set_status(f"出错：{err}")
            messagebox.showerror(APP_TITLE, str(err))
            return
        if done:
            try:
                done(res)
            except Exception as e:                       # noqa: BLE001
                messagebox.showerror(APP_TITLE, str(e))

    @staticmethod
    def _set_text(widget, text):
        state = widget.cget("state")
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state=state if state else "normal")

    def _tick(self):
        """每 2 秒刷一次运行状态和日志"""
        alive = daemon_alive()
        if alive:
            lock = read_json(DAEMON_LOCK, {}) or {}
            hb = lock.get("heartbeat") or "—"
            self.lbl_run.configure(text=f"● 监听中（pid {lock.get('pid')}，心跳 {hb}）",
                                   style="Ok.TLabel")
            self.lbl_stats.configure(text="统计：" + self._stats_text())
        else:
            self.lbl_run.configure(text="○ 未运行", style="Bad.TLabel")
            self.lbl_stats.configure(text="统计：" + self._stats_text())

        # 日志尾部
        try:
            if os.path.exists(DAEMON_LOG):
                size = os.path.getsize(DAEMON_LOG)
                if size != self._log_seen:
                    self._log_seen = size
                    with open(DAEMON_LOG, encoding="utf-8", errors="replace") as f:
                        lines = f.read().splitlines()[-400:]
                    self._set_text(self.txt_log, "\n".join(lines))
                    self.txt_log.see("end")
        except OSError:
            pass

        self.after(2000, self._tick)

    def _stats_text(self):
        s = read_json(STATS_PATH, {}) or {}
        if not s:
            return "还没有统计"
        return (f"已回复 {s.get('sent', 0)} 条 · 生成 {s.get('llm_calls', 0)} 次 · "
                f"发送失败 {s.get('send_failed', 0)} · 模型出错 {s.get('llm_errors', 0)}")


def main():
    # 高 DPI 下文字不发虚
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
