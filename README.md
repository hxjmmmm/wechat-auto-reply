# 微信自动回复助手（通用工具版）

把原来只服务一个人的脚本，做成了一台**带界面的通用工具**：
自动找到本机微信 → 选一个联系人 → 填一个 API Key → 改提示词 → 开始监听。

**打包成 exe 后，目标机器不需要装 Python、不需要装任何依赖** —— 装了微信并登录过就行。

| 页签 | 干什么 |
|---|---|
| ① 环境 & 账号 | 自动探测微信安装目录、数据目录、登录过的账号、外部工具路径 |
| ② 联系人 | 搜人 / 拉最近会话，点一下选中要回复的对象 |
| ③ 模型 API | 选服务商（智谱免费 / DeepSeek / 通义 / OpenAI）→ 贴 key → 一键测连通 |
| ④ 提示词 | 改人设和语风，保存即生效 |
| ⑤ 运行 & 日志 | 启停常驻监听、看统计、看实时日志、设开机自启 |

项目位置：`D:\Program Files (x86)\自动化\wechat-assistant`

## 怎么跑起来

### 方式一：直接双击 exe

```
dist\微信自动回复助手.exe
```

1. 「① 环境 & 账号」→ 点 **自动检测微信**
2. 选账号 →「② 联系人」→ 点 **最近会话**，选中要回复的人
3. 「③ 模型 API」→ 选 **智谱 AI（注册即免费）** → 贴 API Key → 点 **测试连接**
4. （可选）「④ 提示词」按自己口味改人设
5. 底部 **保存全部配置** → **▶ 启动监听**

想让它常驻后台：在「⑤ 运行 & 日志」勾上 **开机自动后台运行**，以后登录电脑就自动跑，
不用再打开这个窗口，也不需要 WorkBuddy 开着。

### 方式二：源码跑

```bash
python launcher.py                 # 开界面
python launcher.py --daemon-run    # 后台常驻（无界面）
python launcher.py --script decrypt_image.py -- --minutes 10   # 单独跑某个伴随脚本
```

### 打包 exe

```bash
.build\venv\Scripts\python.exe build_exe.py
```

产物：`dist\微信自动回复助手.exe`（单文件，已含 Python 运行时 + 全部依赖）。
首次打包前需要先建好打包环境：

```bash
python -m venv .build\venv
.build\venv\Scripts\python.exe -m pip install pyinstaller click pycryptodome zstandard uiautomation pywin32 comtypes
.build\venv\Scripts\python.exe -m pip install -e "<wechat-local-butler 技能目录>\tool"
```

### 界面之外：几个纯命令行动作

```bash
微信自动回复助手.exe --daemon-run    # 不开界面，直接后台监听
微信自动回复助手.exe --stop-daemon   # 让正在跑的常驻进程停下
微信自动回复助手.exe --selftest      # 自检：报出微信环境、脚本定位、界面能否建起来
微信自动回复助手.exe --script <脚本名> -- <参数...>   # 用 exe 跑内置的伴随脚本
微信自动回复助手.exe --version
```

### 「自动获取微信」都探测了什么

全是只读探测，不动微信任何文件：

| 项目 | 怎么找到的 |
|---|---|
| 微信安装目录 / 版本 | 注册表 `HKCU\Software\Tencent\Weixin` → `InstallPath` |
| 图片解密要用的 DLL | 安装目录下的版本子目录（含 `VoipEngine.dll` 的那个） |
| 账号 code | `%APPDATA%\Tencent\xwechat\net\kvcomm\key_<code>_*.statistic` 文件名 |
| 数据根目录 | 各盘「文档」目录下的 `xwechat_files`（会用注册表里的实际文档路径，不猜） |
| 登录过的账号 | 数据根目录下的 `wxid_*` 子目录（按数据新旧排序） |
| 联系人 / 最近会话 | 调 `wechat-cli contacts` / `sessions` |

### 首次运行会先「建基线」

刚装好的时候，聊天记录里躺着几百条旧消息。程序**第一轮只把现有消息标记为已读**，
从下一条新消息才开始回复 —— 否则一装上就会把历史消息全回一遍。
想手动重来一次（比如换了联系人），点「⑤ 运行 & 日志」里的**重建基线**。

### 分享给别人时

`dist\微信自动回复助手.exe` 是**单文件**，直接发过去就行 —— 对方不需要装 Python、
不需要装 wechat-cli、不需要装任何依赖，只要他电脑上装了微信并登录过。

注意别把 `config.json` 一起发出去（里面有你自己的 API Key 和联系人）。
`dist\config.示例.json` 是干净模板。

对方第一次打开后照着「怎么跑起来」走一遍即可（检测 → 选人 → 填 key）。

---

以下为底层实现细节（改代码前值得先看）。

## 运行架构（2026-09-16 起：常驻脚本，空闲 0 token）

**核心思路：负责"等消息"的必须是不含模型的进程，模型只在真有消息时才被叫醒一次。**

```
daemon.py（常驻后台，纯 Python）
  └─ 每 20 秒读一次本地库          ← 不花任何 token
     └─ 只有她真的发了消息
        └─ reply_engine.py 发一次几百 token 的 HTTP 请求生成回复
           └─ send_v2.py 真实键鼠发送 → 读库校验 → 成功才清待回复队列
```

被叫醒的次数 = 她实际发消息的次数。以前那套「WorkBuddy 定时任务叫醒模型，
模型反复调 `monitor.py --wait` 去等」的方案，空闲一小时要烧 11～12 次模型调用
（见 `monitor.py` 的 `--wait` 说明），现已降为 **0**。

| 文件 | 作用 |
|---|---|
| `daemon.py` | 常驻监听 + 自动回复主程序（看门狗式，异常不退出） |
| `reply_engine.py` | 直接 HTTP 调大模型生成回复（只依赖标准库，不带 agent 壳） |
| `monitor.py` | 检测逻辑（daemon 直接复用它的函数，不再需要 `--wait` 阻塞模式） |

### 配置模型（`config.json` 的 `llm` 段）

默认用**智谱 AI 的免费模型**（手机号注册即可，文本 + 视觉都免费）：

```json
"llm": {
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "api_key": "你的key",
  "model": "glm-4-flash",        // 文本，永久免费
  "vision_model": "glm-4v-flash" // 她会发图片，留空则不做图像理解
}
```

想换别的（都是 OpenAI 兼容接口，改这三行就行）：

| 服务 | base_url | 模型 | 备注 |
|---|---|---|---|
| 智谱（默认） | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` / `glm-4v-flash` | 免费，含视觉 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | 约 ¥1/百万输入 token，无视觉 |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-flash` | 每月免费额度 |

key 也可以放环境变量 `WECHAT_LLM_KEY`，不必写进文件。

### 常用命令

```bash
python daemon.py --test-llm          # 用一句假消息试模型，验 key
python daemon.py --once --no-send    # 只检测 + 生成回复，不发送
python daemon.py --status            # 看是否在跑、统计、待回复条数
pythonw daemon.py                    # 正式常驻（无窗口）
```

### 开机自启与排障

启动器在 **「启动」目录**：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\wechat-auto-reply-daemon.cmd`
（项目里的 `start-daemon.cmd` 是同一份，双击可手动拉起）。
登录 Windows 就会自动跑，不需要 WorkBuddy 开着。

> 为什么不用计划任务：这台机器上 `schtasks.exe` 在安全黑名单里，COM 建快捷方式也被拦，
> 只能用启动目录。启动器里含中文路径，**必须以系统 ANSI(GBK) 编码保存**，否则 cmd 读成乱码。

排障看三个文件：

| 文件 | 看什么 |
|---|---|
| `data/daemon.log` | 每轮唤醒 / 生成 / 发送的完整记录 |
| `data/daemon_crash.log` | 进程崩溃的 traceback（`pythonw` 没有控制台，不加这层什么都看不到） |
| `data/monitor.log` | 更底层的检测、图片解密、语音转写日志 |

常见情况：
- **`stage: abort`**：发送时微信窗口没到前台（多数是锁屏/息屏）。这是安全闸，一个字都不会发，
  消息留在队列里，解锁后自动补发。
- **锁屏期间**：读消息一切正常（纯读库），只有"发"这个动作做不了。
- **`daemon_running: false`**：进程掉了。双击 `start-daemon.cmd` 重新拉起即可。

## 目录结构

```
wechat-assistant/
├── launcher.py       总入口：开界面 / 后台常驻 / 用自己跑伴随脚本（打包 exe 的入口）
├── gui.py            图形界面（五个页签，纯 tkinter 无依赖）
├── wxenv.py          自动探测微信环境（注册表 / 数据目录 / 账号 / 外部工具 / 联系人）
├── runner.py         统一「怎么调伴随脚本」，打包成 exe 后靠它自包含运行
├── build_exe.py      一键打包成单文件 exe（PyInstaller）
├── daemon.py         常驻监听 + 自动回复主程序（空闲 0 token，看门狗式不退出）
├── reply_engine.py   直接 HTTP 调大模型生成回复（OpenAI 兼容接口，只用标准库）
├── prompt.txt        提示词（界面「④ 提示词」改这个；不存在则用内置默认）
├── start-daemon.cmd  手动拉起常驻进程（新版建议直接双击 exe）
├── monitor.py        检测新消息（指纹增量 + 待回复队列 + 自动解密图片 + 语音转写 + 阻塞等待）
├── decrypt_image.py  解密微信图片（V2 .dat → jpg/png），供"看图"用
├── voice_text.py     读语音消息的「转写文字」（解库取 packed_info_data）
├── verify_sent.py    发送后校验：读库确认消息落在目标会话（替代截图 OCR）
├── send_v2.py        发送：激活窗口 → 搜索导航 → 拆条发送（可读库校验落点）
├── watch.py          早期版本的常驻监听（已被 daemon.py 取代，保留备用）
├── notify.py         Windows 桌面通知
├── ocr.ps1           调用 Windows 系统 OCR（发送前截图校验，默认已不用）
├── _shot.ps1         截图（支持指定区域）
├── config.json       联系人、路径、模型等配置（界面里改，不手写）
├── STYLE.md          回复语风规则说明
├── README.md         本文件
├── .build/           打包环境（venv + PyInstaller 中间产物，可删，删了要重建）
├── dist/             打包产物
│   ├── 微信自动回复助手.exe    单文件，目标机不需要装 Python
│   └── config.示例.json        干净模板（分享给别人时用这份）
└── data/             运行时数据（脚本自动读写，不用手动管）
    ├── state.json           已见消息指纹 + 基线标记
    ├── pending.json         待回复队列（未确认发送成功的消息）
    ├── check_result.json    最近一次检测结果（被唤醒时才写）
    ├── wait_status.txt      等待模式的最后状态行（纯 ASCII，供程序化判断）
    ├── shift.json           本轮值守的起点（满 --shift-minutes 自动清除）
    ├── decrypted.json       图片解密结果
    ├── voice_text.json      语音转写结果
    ├── images/              解密出来的图片（自动清理 24h 前的）
    ├── reply.txt            待发送消息（每条一行）
    ├── inbox.jsonl          历史新消息归档
    ├── monitor.log          检测日志
    ├── daemon.log           常驻进程日志（每轮唤醒/生成/发送都记在这）
    ├── daemon_crash.log     进程崩溃时的 traceback（无窗口进程会吞掉报错，靠它排障）
    ├── stop.flag            停止标记（界面点「停止」时写，进程看到就退出）
    ├── daemon.lock          常驻进程心跳（防重复启动 + 界面判断在不在跑）
    ├── daemon_stats.json    调用次数/发送次数统计
    ├── send_result.json     最近一次发送的完整返回
    ├── selftest.txt         自检结果（--selftest 写的）
    └── verify*.png/txt      发送前的校验截图与 OCR 结果
```

## 三类消息的读取能力

| 类型 | 能否读到内容 | 方式 |
|---|---|---|
| 文字 | ✅ | 直接读本地数据库 |
| 图片 | ✅ | `decrypt_image.py` 解密 `.dat`（**不碰屏幕，息屏也能用**） |
| 语音 | ✅ | `voice_text.py` 读数据库里的转写文字（**不碰屏幕，息屏也能用**） |

### 语音：转写文字其实是落盘的

音频文件确实不落盘（`VoiceTemp` 会被清空，`msg\attach\` 下只有 `Img`，根本没有 `Voice`），
但**微信「语音转文字」的结果是写进数据库的** —— 在消息表的 `packed_info_data` 字段里，
protobuf 编码。之前全盘搜索搜不到，纯粹是因为数据库文件是加密的，明文只在解密后存在。

所以语音不需要截图、也不需要 ASR，直接解库读即可。

```powershell
$DPY = "C:\Users\<你的用户名>\.workbuddy\skills\wechat-local-butler\tool\.venv\Scripts\python.exe"
& $DPY -u voice_text.py --minutes 60 --out "data\voice_text.json"
```

`monitor.py` 检测到 `[语音]` 消息时自动调用，结果挂在检测结果的 `voice_text` 字段上，
`text` 会变成 `[语音] 她说的原话`。

注意：转写由微信服务端异步生成，刚收到的语音可能还没有文本。此时消息会带
`voice_pending: true`，`monitor.py` 会放进待回复队列，后续轮次重试（最多 6 轮）。

## 省 token：等待在脚本里做，模型只在真有消息时才醒

一开始的做法是「模型每 45 秒跑一次检测、看一眼结果」，一次空闲小时要几十次模型调用，
token 全烧在「看有没有消息」这件不需要脑子的事上。

现在检测循环整个搬进 `monitor.py --wait`：一次调用最长阻塞 7 分钟，期间脚本自己每 20 秒查一次，
**只有真的等到了该回复的消息才返回**。模型一轮只需要一次调用，而且大部分时候什么都不用读。

```
        旧：模型 ←每 45s→ monitor.py           一小时 ≈ 69 次模型调用
        新：模型 ---- monitor.py --wait ----   一小时 ≈  8 次模型调用（其余时间模型完全不参与）
                        ↑ 内部每 20s 自己轮询
```

`--wait` 结束时会打一行 ASCII 状态（同时写 `data/wait_status.txt`，供拿不到 stdout 时读）：

| 状态行 | 含义 | 该做什么 |
|---|---|---|
| `WAKE new fresh=N` | 她发来了新消息 | 读结果 → 看图 → 写回复 → 发送 |
| `WAKE voice ready=N` | 语音转写结果出来了 | 同上（这是之前先跳过的那条语音） |
| `WAKE pending pending=N` | 上轮没发成功的遗留消息 | 重试发送 |
| `NO_NEW polls=N waited=Ns` | 这一整段没有要回的内容 | 直接再调一次 `--wait` 继续盯 |
| `SHIFT_OVER` | 本轮值守已满 `--shift-minutes` 分钟 | 收工总结 |
| `BUSY` | 已经有一个监听在跑 | 直接结束本次任务，不要重试 |

`data/watch.lock` 是一把互斥锁（带心跳，超过 120 秒没更新视为失效）：
定时任务万一叠着触发两轮，后一轮会立刻返回 `BUSY` 退出，不会出现两个监听同时盯同一个会话。`

`--shift-minutes 52` 由脚本自己记住本轮值守的起点，所以调用方不需要数轮数、算时间 ——
满 52 分钟就返回 `SHIFT_OVER` 并清掉记录，下一个小时重新开始一轮。

其他几个参数：`--poll 20`（轮询间隔秒）、`--wait-timeout 280`（单次调用最长等待，要留够工具超时余量）、
`--pending-retry 300`（遗留消息至少隔这么久才再唤醒一次，避免锁屏期间空转）。

## 日常使用

全自动已挂定时任务：触发后用 `--wait` 阻塞轮询，有消息就回，约 52 分钟后收工。
模型只在真的有消息时才参与，空闲时间基本不消耗 token。

手动查看 / 调试：

```powershell
Set-Location "D:\Program Files (x86)\自动化\wechat-assistant"
$PY  = "C:\Users\<你的用户名>\.workbuddy\binaries\python\versions\3.13.12\python.exe"                          # 检测
$DPY = "C:\Users\<你的用户名>\.workbuddy\skills\wechat-local-butler\tool\.venv\Scripts\python.exe"            # 解密/读库
$SPY = "C:\Users\<你的用户名>\.workbuddy\skills\wechat-send-new\scripts\.venv\Scripts\python.exe"             # 发送

# 只用一次检测（不阻塞，看当前状态）
& $PY -u monitor.py --context 16 --out "data\check_result.json"

# 阻塞等待：脚本自己轮询，等到该回复的消息才返回（省 token 的用法）
& $PY -u monitor.py --wait --shift-minutes 52 --wait-timeout 280 --poll 20 --context 16 --out "data\check_result.json"

# 重建基线（把当前所有消息视为已读，之后不再触发）
& $PY -u monitor.py --init

# 手动发送（每条消息写一行放进 data\reply.txt）
& $SPY -u send_v2.py --contact "<联系人昵称>" --message-file "data\reply.txt" --split --yes --no-verify --verify-after

# 单独看某段时间的语音说了什么
& $DPY -u voice_text.py --minutes 60
```

## 发送流程与安全机制

```
随机等待 20～90s → 激活窗口 → 点搜索框、贴名字、回车 → 逐条发送 → 读库校验落点
```

**两种校验方式，按需选：**

| 方式 | 参数 | 时机 | 依赖屏幕 | 精度 |
|---|---|---|---|---|
| 截图 OCR（旧） | 默认 | 发送**前** | ✅ 必须亮着 | 模糊比对标题 |
| 读库校验（新） | `--no-verify --verify-after` | 发送**后** | ❌ 完全不看屏幕 | 精确匹配消息内容 |

推荐用新的组合：搜索词「<联系人昵称>」在通讯录里**全局唯一**（已验证只有 1 条匹配，
`nick_name` 和 `remark` 都是它），导航本身可信；而截图 OCR 是整条链路里唯一依赖
屏幕的环节，也是息屏时发送失败的唯一原因。

`verify_sent.py` 做的校验比 OCR 硬得多 —— 它直接查她的 `Msg_<md5(wxid)>` 表，
看刚发出去的那几句话是不是真的在里面。命中 = 100% 确定发对人；没命中 = 导航进了
别的会话，立刻告警。

```powershell
# 只导航，不发送（演练）
& $SPY -u send_v2.py --contact "<联系人昵称>" --no-verify

# 正式发送：跳过截图，发送后读库确认
& $SPY -u send_v2.py --contact "<联系人昵称>" --message-file "data\reply.txt" --split --yes --no-verify --verify-after
```

返回码：`0` 成功 / `2` 校验失败未发送 / `3` 已发送但**没落在她的会话**（要人工处理）。

## 息屏 / 锁屏能不能用

要拆开看 —— **读**完全不依赖屏幕，**发**必须有活着的交互式桌面：

| 状态 | 读消息 | 发送 |
|---|---|---|
| 只关显示器（系统醒着、没锁屏） | ✅ | ✅ |
| 已锁屏（Win+L / 自动锁屏） | ✅ | ❌ → 进队列，解锁后补发 |
| 睡眠 / 休眠 | ❌ 进程都停了 | ❌ → 唤醒后补发 |

### 锁屏为什么发不出去（实测结论，不是实现没做好）

锁屏后桌面会切到 Winlogon 的**安全桌面**，这是 Windows 的安全边界。实测：

- `SendInput` / `mouse_event`（真实键鼠注入）会被路由到锁屏界面，送不到微信
- 截图全白/全黑 —— `CopyFromScreen` 拿不到画面
- `GetCursorPos` / `SetCursorPos` 直接返回 **Access denied**
- **连"绕开前台"的路也堵死**：实测锁屏下用 `PostMessage` 直投窗口消息，
  WM_CHAR / WM_IME_CHAR / VK_PACKET 三种写法都试过，还伪造了 `WM_ACTIVATE`，
  **键盘输入完全无效**；又用 `PostMessage` 模拟鼠标点击会话列表 3 次，
  未读数一个都没清零 —— **鼠标同样无效**。
  原因：微信 4.x 是 Qt（`Qt51514QWindowIcon`）+ MMUI 自绘，
  Qt 在窗口失去激活后就不再处理 UI 事件。
- UIAutomation 也是死路：元素树只有 `WindowControl '微信'` → `PaneControl 'MMUIRenderSubWindowHW'`，
  再往下全空白，没有任何可操作的控件。

结论：「锁屏也能回」在纯用户态**没有可行解**（除非注入微信进程改内存、
或改用第三方协议端登录 —— 两者都有封号风险，不建议）。

### 能解决的是「息屏」：别让它锁屏

电源设置里把**睡眠设成「从不」**、**关掉自动锁屏**（屏保关闭 / 唤醒不需要密码），
只让显示器按时关闭。这样显示器是黑的（省电，体感和锁屏一样），
但 Windows 会话一直活着，窗口、鼠标、键盘全部照常工作 —— **全自动照跑**。

### 内置的安全闸

`send_v2.py` 点完搜索框后会检查微信窗口是否真的到了前台，没到就立刻中止
（`stage: abort`，一个字不发，返回码 2）。防的正是锁屏这种"点击落空后按键乱飞"的
场景 —— 宁可不发，也绝不发错人。

不管哪种情况，消息都不会丢 —— 见下方「待回复队列」。

## 图片解密（decrypt_image.py）

微信把聊天图片加密存在 `msg\attach\<md5(对方wxid)>\<年-月>\Img\*.dat`。
解密三步，全部离线，不需要碰微信进程：

1. **取账号 code**：从 `%APPDATA%\Tencent\xwechat\net\kvcomm\key_<code>_*.statistic`
   的文件名里提取（本机为 `211958201`）
2. **派生密钥**：
   - `aes_key = md5(f"{code}{清洗后的自己wxid}").hexdigest()[:16]`
   - `xor_key = code & 0xFF`
3. **解 V2 容器**：跳过 15 字节头 → AES-128-ECB 解前 `aes_size` 字节 → 跳过 16 字节分隔
   → 剩余部分逐字节 XOR

解出来可能是标准 jpg/png，也可能是微信自研的 **wxgf** 格式。后者要调微信自带的
`VoipEngine.dll` 里的 `wxam_dec_wxam2pic_5` 转码（见下方坑位）。

用法：

```powershell
$DPY = "C:\Users\<你的用户名>\.workbuddy\skills\wechat-local-butler\tool\.venv\Scripts\python.exe"
# 按消息时间精确取图（几条图片消息就传几个 --at）
& $DPY -u decrypt_image.py --at "2026-09-15 17:31" --out "data\decrypted.json"
# 或按最近 N 分钟批量取
& $DPY -u decrypt_image.py --minutes 30 --max 4 --out "data\decrypted.json"
```

`monitor.py` 检测到 `[图片]` 消息时会自动调用它，结果放进检测结果的 `new_images` 字段。

## 待回复队列（防止丢消息）

消息一被检测到就会标记为「已见」。如果随后发送失败（息屏、校验拦截、进程中断），
这条消息**再也不会被报出来** —— 等于被悄悄吞掉。

所以 `monitor.py` 会把检测到的消息同时写入 `data/pending.json`：

- 后续每次检测都会把队列里未清掉的消息**重新报为待回复**
- 发送成功并回读确认后，调 `monitor.py --clear-pending` 清空
- 自动愈合：若发现「她那条待回消息之后我已经发过消息」，自动从队列移除，避免重复回
- 等待模式下**只有「本轮新检测到」的消息会立刻唤醒**（`fresh_count > 0`）；
  队列里的遗留消息不会每轮都唤醒，而是每隔 `--pending-retry` 秒唤醒一次重试 ——
  这样锁屏发不出去的期间不会疯狂空转烧 token，解锁后最多等 5 分钟就补发
- 语音消息若转写还没生成，会先进队列等待；等转写结果到手时返回 `WAKE voice`，
  只唤醒一次（不会因为反复重试而空转）

## 踩过的坑（改代码前先看）

- **`Alt+A` 不能用**：被本机的 Clash Verge 全局热键抢占，按下去弹的是 VPN 注销对话框
- **微信 4.x 是自绘界面**：UIAutomation 读不到任何控件，只能靠键盘 + 视觉校验
- **微信主窗口 `IsWindowVisible()` 可能为 0**（最小化到托盘），且 Qt 无边框窗口没有 `WS_CAPTION`，枚举时这两种过滤都不能用
- **窗口类名**：`Qt51514QWindowIcon` 是微信主窗口；`Qt5QWindowIcon` 是 QQ 托盘，别选错
- **`Ctrl+F` 不可靠**：窗口已打开时经常无效，改用鼠标点击搜索框（相对窗口偏移固定 `170, 50`，与窗口大小无关）
- **PowerShell 管道会毁中文编码**：原生命令的 UTF-8 输出会被按 GBK 二次解码。一律让 Python 脚本直接写文件再用 Read 读取 —— 这正是 `monitor.py --out` 存在的原因
- **PowerShell 工具的 stdout 不回传**：所以所有脚本都改成写文件再读
- **WinRT OCR 异步**：必须先 `Add-Type -AssemblyName System.Runtime.WindowsRuntime`，再用 `AsTask()` 泛型辅助函数等待，直接 `.GetAwaiter()` 会报"不包含 AsTask 方法"
- **traceback 要取尾部**：错误的关键信息（异常类型）在最后一行，只取前 300 字符会看到一堆无用的调用栈框架
- **`.dat` 不是老式单字节 XOR**：头是 `07 08 56 32`（V2 格式），老教程那套简单异或完全解不开
- **`wxam_dec_wxam2pic_5` 第 5 个参数不能传 NULL**：会直接 access violation 崩掉进程。它是
  32 字节配置缓冲区，中间有 `AA AA AA AA` 标志位（见 `_WXGF_ARG5`）
- **输出容量要预置**：第 4 个参数是 in/out 的 `uint32` 容量，初值必须给 `0x7CFB00`，
  传 0 会拿不到任何数据
- **高清图常常是 PNG、显示版才是 wxgf**：同一个 md5 有 `_h`（高清）/无后缀（显示版）/`_t`（缩略图）
  三档，`_h` 解密后直接就是标准 PNG，不需要 DLL 转码。选图优先 `_h`
- **WCDB 的列可能是 `bytes` 也可能是 `str`**：SQLite 里 `message_content` 两条文本走的是
  未压缩字符串、图片/语音走的是 zstd blob，处理前要统一成 bytes
- **语音转写不在 `message_content` 里**：那里只有 `<voicemsg>` XML（时长、aeskey）。
  真正的转写文本在 **`packed_info_data`**（protobuf）。别再只盯着 `message_content`。
- **`packed_info_data` 别用固定偏移硬解**：`08 16 | 10 02 | 2a <len> [08 02 | 12 <len> <文本>] | 58 00`
  只是当前版本的样子。用 `voice_text.py` 里的通用 protobuf  walker 递归找「含中文且无控制字符」
  的最长 UTF-8 串，版本变了也不会挂
- **protobuf 的长度字节可能刚好是可打印 ASCII**（如 `0x30` = `'0'`），会让整段脏数据
  通过 `decode('utf-8')` 且包含中文。必须用控制字符正则把这类候选刷掉（见 `CTRL` 正则）
- **同一分钟的多条语音要按序号配对**：消息时间戳只精确到分钟，转写结果有时间。
  `monitor.py` 里用「同分钟内的第几条」下标配对，别简单地用 `{time: text}` 覆盖
- **语音方向判定**：`real_sender_id` 是 `Name2Id.rowid`，不是 wxid。
  <联系人昵称> = 22，我 = 2

## 依赖环境

- 读取：`C:\Users\<你的用户名>\.workbuddy\skills\wechat-local-butler\tool\.venv\Scripts\wechat-cli.exe`
- 解密图片 / 读语音转写：同上那个 venv 的 `python.exe`（含 pycryptodome / zstandard /
  wechat_cli），配置在 `config.json` 的 `decrypt_python`
- 发送：`C:\Users\<你的用户名>\.workbuddy\skills\wechat-send-new\scripts\.venv\Scripts\python.exe`（含 uiautomation / pywin32）
- 数据库目录：`D:\Users\Documents\xwechat_files\wxid_xxxxxxxxxxxx_c382\db_storage`
  （机器上有两个账号目录，另一个 `wxid_zzzzzzzzzzzzzz_e9f6` 已不活跃，勿用）

技能本体装在 C 盘的 `~/.workbuddy/skills/`，项目文件本身都在 D 盘。

## 隐私与安全

- 所有解密与读取都在**本机**完成，工具代码无任何网络上传行为。
- 密钥文件在 `~/.wechat-cli/all_keys.json`，**不要**分享、同步或提交到版本库。
- 微信升级或重启后密钥可能失效，重跑 `wechat-cli init --force --db-dir "<db_dir>"`。
- `data/` 下的文件含聊天内容，注意不要外传。
