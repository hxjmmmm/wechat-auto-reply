# 微信自动回复助手

监控一个指定联系人的微信消息，用大模型生成回复，通过真实键鼠操作发出去，
再从本地数据库回读确认消息确实落在了对方会话里。

- **单文件 exe，目标机不需要装 Python**
- 空闲时 **0 token**：只有对方真的发了消息才调一次模型
- 全程在本机完成，代码无任何网络上传行为

---

## 一、怎么用（推荐：直接下载 exe）

到 [Releases](https://github.com/hxjmmmm/wechat-auto-reply/releases) 下载
`微信自动回复助手.exe`，放到任意文件夹，双击打开界面，按 4 步走（微信要开着并已登录）：

1. **① 环境 & 账号** → 点「自动检测微信」
   （自动找你这台机器的微信安装目录、数据目录、账号、外部工具）
2. **② 联系人** → 点「最近会话」，双击选中要自动回复的人
3. **③ 模型 API** → 选服务商 → 贴 API Key → 点「测试连接」
   推荐智谱：`open.bigmodel.cn` 手机号注册，文本和视觉模型都免费
4. 底部「保存全部配置」→「▶ 启动监听」

想让它开机自己跑：⑤ 页签勾上「开机自动后台运行」。

> 第一次打开 Windows SmartScreen 会拦截（exe 未签名），
> 点「更多信息 → 仍要运行」。

### 命令行方式

```bat
微信自动回复助手.exe --daemon-run        :: 后台常驻监听
微信自动回复助手.exe --daemon-run --once --no-send   :: 只跑一轮，只生成草稿不发送
微信自动回复助手.exe --daemon-run --status           :: 看运行状态和统计
微信自动回复助手.exe --stop-daemon       :: 停止常驻监听
微信自动回复助手.exe --selftest          :: 自检（ssl / 界面 / 脚本定位）
```

---

## 二、源码方式（想改代码才需要）

```bash
git clone https://github.com/hxjmmmm/wechat-auto-reply.git
cd wechat-auto-reply

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

cp config.example.json config.json     # 复制一份配置再填
python gui.py                          # 打开界面
```

**环境要求**：Windows 10/11 + Python 3.10 及以上（`tkinter` 用官方安装包自带的即可）。
装了微信 PC 版 4.x 且当前处于登录状态。

**一个前提**：读微信数据库依赖 `wechat_cli` 包，这个包**不在 PyPI 上**
（`requirements.txt` 里已注释掉）。两种办法：

- 在配置里把 `wechat_cli` 指向一个可用的 `wechat-cli.exe`（推荐，最省事）
- 或自行获取 `wechat_cli` 包放进 Python 环境

如果只是想用，**请直接用 Release 的 exe**，里面已经内置好了。

---

## 三、打包 exe

```bash
pip install -r requirements-build.txt
python build_exe.py
```

产物 `dist\微信自动回复助手.exe`（约 24 MB）。

打包脚本做了两件容易踩坑的事，改动时别破坏：

- **DLL 搜索顺序**：打包前把解释器自带的 `Library\bin` / `DLLs` 顶到 `PATH` 最前。
  否则 PyInstaller 会按 PATH 抓到别的软件自带的 `libssl-3-x64.dll`（版本对不上），
  打出来的 exe `import ssl` 直接失败，HTTPS 全废。
- **子进程定位程序目录**：exe 会用外部 python 重入跑内置脚本，这种子进程的
  "脚本所在目录" 是 `%TEMP%\_MEIxxxx`。靠 `runner.app_dir()` + 环境变量 `WX_APP_DIR`
  传递真实目录，否则读不到 `config.json`。

打完会自动跑一次 `exe --selftest` 校验 HTTPS 和界面。

---

## 四、现在的运行架构

```
daemon.py（常驻后台）
  └─ 每 N 秒读一次本地微信数据库        ← 不花任何 token
     └─ 检测到对方有新消息
        ├─ reply_engine.py  → 调一次大模型生成回复
        └─ send_v2.py       → 真实键鼠发送 → verify_sent.py 读库校验
                              └─ 确认落在对方会话 → 才清空待回复队列
```

| 文件 | 作用 |
|---|---|
| `launcher.py` | 总入口：开界面 / 后台常驻 / 用自己当解释器跑伴随脚本 |
| `gui.py` | 图形界面（五个页签，纯 tkinter） |
| `daemon.py` | 常驻监听主程序，看门狗式，单轮异常不退出 |
| `monitor.py` | 检测逻辑：新消息 / 待回复队列 / 基线 |
| `reply_engine.py` | 调大模型生成回复（OpenAI 兼容接口，只用标准库 urllib） |
| `send_v2.py` | 微信窗口导航 + 键鼠发送 + 发送后校验 |
| `verify_sent.py` | 读数据库确认消息落在了对方会话 |
| `decrypt_image.py` | 解密微信图片 `.dat`（供视觉模型使用） |
| `voice_text.py` | 读取语音消息的转写文字 |
| `wxenv.py` | 自动探测微信环境（注册表 / 数据目录 / 账号 / 联系人） |
| `runner.py` | 统一「怎么调伴随脚本」，打包后靠它自包含运行 |
| `notify.py` | 桌面通知 |
| `build_exe.py` | 一键打包成单文件 exe |

---

## 五、配置（`config.json`）

复制 `config.example.json` 改名即可。主要字段：

| 字段 | 说明 |
|---|---|
| `contact` / `contact_wxid` | 要自动回复的人（界面里选，不用手填） |
| `my_wxid` | 你自己的 wxid |
| `poll_seconds` | 轮询间隔，默认 20 秒 |
| `auto_send` | `false` 时只生成草稿写到 `data/reply.txt`，不碰微信 |
| `notify` | 有动作时是否弹桌面通知 |
| `pet_prefix` | 回复开头爱用的称呼（如 `宝宝`），留空则不加 |
| `llm` | 模型服务：`base_url` / `api_key` / `model` / `vision_model` |
| `db_dir` / `account_code` | 由「自动检测微信」填，一般不用手改 |

key 也可以放环境变量 `WECHAT_LLM_KEY`，不写进文件。

换服务商（都是 OpenAI 兼容接口，改三行）：

| 服务 | base_url | 模型 |
|---|---|---|
| 智谱（默认） | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` / `glm-4v-flash` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-flash` |

> **改完配置要重启监听** —— `config.json` 只在进程启动时读一次。

---

## 六、发送安全机制

- **发送前**：导航到搜索结果后校验目标会话，不对就不落字
- **发送后**：读数据库确认消息真的在对方会话里；确认了才清队列
- **校验脚本自身出错** ≠ 发错会话。这种情况记为 `sent-verify-error`，
  会清掉队列并提示人工确认，避免下一轮重复发送
- **锁屏时发不出去**：Windows 安全桌面会吞掉模拟输入。此时一个字都不发，
  消息留在待回复队列，解锁后自动补发

---

## 七、排障

| 文件 | 看什么 |
|---|---|
| `data/daemon.log` | 每轮检测 / 生成 / 发送的完整记录 |
| `data/monitor.log` | 更底层的检测、图片解密、语音转写日志 |
| `data/sent_check.json` | 发送后读库校验的明细 |
| `data/send_result.json` | 发送子进程的原始返回 |

```bash
python daemon.py --test-llm          # 用一句假消息试模型，验 key
python daemon.py --once --no-send    # 只检测 + 生成回复，不发送
python daemon.py --status            # 是否在跑、统计、待回复条数
```

常见情况：

- `stage: abort` —— 微信窗口没到前台（锁屏 / 息屏），安全闸生效，未发送
- `sent-wrong-session` —— 可能发到了别的会话，**请人工检查**
- 微信升级或重启后密钥可能失效：重跑一次「自动检测微信」或 `wechat-cli init --force`

---

## 八、隐私

- 所有读取、解密都在**本机**完成，代码无任何网络上传行为
- `config.json`（含 API Key、双方 wxid、本机路径）与 `prompt.txt`（自定义提示词）
  都已在 `.gitignore` 中，**不会进仓库，也不会被打进分发的 exe**
- `data/` 下存着聊天内容，注意不要外传
- 微信密钥文件（`all_keys.json`）不要分享或同步

---

## 许可

仅供个人学习研究使用，请遵守微信软件许可协议与当地法律法规。
