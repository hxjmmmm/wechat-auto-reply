# -*- coding: utf-8 -*-
"""reply_engine.py — 直接用 HTTP 调大模型生成回复（不经过任何 agent 壳）

为什么单独写这个：常驻监听必须能"自己叫模型"，而且要极便宜 ——
一次只发几百 token 的小请求，而不是拉起一个带完整系统提示词的 agent。
只依赖标准库（urllib），不需要装任何包。

配置（config.json 里的 llm 段）：
    "llm": {
      "enabled": true,
      "base_url": "https://api.deepseek.com/v1",   # OpenAI 兼容接口的根地址
      "api_key": "sk-...",
      "model": "deepseek-chat",
      "vision_model": "",        # 留空＝不用视觉；填了就用它处理图片（如 qwen-vl-max）
      "temperature": 1.3,
      "max_tokens": 200,
      "max_lines": 3
    }
"""
import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# 提示词外置成 prompt.txt —— 图形界面「提示词」页签改的就是这个文件。
# 文件不存在或为空时，回落到下面这份内置默认提示词。
# 注意：打包成 exe 后 __file__ 指向临时解压目录，用户改的 prompt.txt 必须
# 落在 exe 同级目录，否则每次运行都会被重置。
try:
    import runner as _runner
    PROMPT_DIR = _runner.app_dir()
except Exception:
    PROMPT_DIR = HERE
PROMPT_PATH = os.path.join(PROMPT_DIR, "prompt.txt")

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# 「哈哈」「嗯嗯」「好」这类纯应答 —— 对它喊称呼很生硬，这种情况才允许省掉前缀
INTERJECTION_RE = re.compile(r"^(哈+|嗯+|哦+|好+|行+|啊+|呀+|诶+|呃+|对+|是+|嘿+|嘻+)[~～。.!！?？…]*$")

# 内置默认提示词 —— 刻意保持中性，不含任何个人信息
# （没有名字、称呼、关系、口头禅，因为它们会跟着 exe 一起被分发出去）。
# 使用者请在界面「提示词」页签里换成自己的；一旦写了 prompt.txt 就以那份为准。
SYSTEM_PROMPT = """你是在用手机微信回消息的真人，不是助手、不是客服、不是 AI。
用日常口语回，像本人随手打的字。

【基本规则】
1. 每条消息很短：2 到 10 个字。
2. 一次 1 到 3 条，每条独立成一行。
3. 一句只说一件事，想到什么说什么，不需要逻辑连贯，也不需要完整句子。
4. 可以省略主语和标点，结尾不加句号，不用 emoji。

【别这么回】
- 把对方刚说的话原样重复一遍
- 客套、鼓励、总结（"加油""会好的""开心就好"）
- 复述对方的话（"听起来你今天挺累的"）
- 反问式关心（"需要我做什么吗？"）

【输出】只输出要发的消息本身，每条一行。不要序号、不要引号、不要解释。"""

# 内置默认提示词。改语气请改这个常量，或直接在图形界面里改（会写到 prompt.txt）
DEFAULT_PROMPT = SYSTEM_PROMPT


def load_prompt():
    """读提示词：优先 prompt.txt，没有/为空就用内置默认值"""
    try:
        with open(PROMPT_PATH, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    return DEFAULT_PROMPT


def save_prompt(text):
    """把提示词写进 prompt.txt（图形界面「保存提示词」调它）"""
    with open(PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write((text or "").rstrip() + "\n")


def prompt_is_custom():
    """是否用了自定义提示词（界面上显示提示用）"""
    try:
        with open(PROMPT_PATH, encoding="utf-8") as f:
            return f.read().strip() not in ("", DEFAULT_PROMPT)
    except OSError:
        return False


# ---------------------------------------------------------------- 配置

def llm_cfg(cfg):
    llm = dict(cfg.get("llm") or {})
    llm.setdefault("enabled", bool(llm.get("api_key")))
    llm.setdefault("base_url", DEFAULT_BASE)
    llm.setdefault("model", DEFAULT_MODEL)
    llm.setdefault("vision_model", "")
    llm.setdefault("temperature", 1.3)
    llm.setdefault("max_tokens", 200)
    llm.setdefault("max_lines", 3)
    llm.setdefault("timeout", 90)
    # 允许用环境变量兜底，避免 key 写进文件
    if not llm.get("api_key"):
        llm["api_key"] = os.environ.get("WECHAT_LLM_KEY", "")
    if not llm.get("base_url"):
        llm["base_url"] = os.environ.get("WECHAT_LLM_BASE", DEFAULT_BASE)
    return llm


def resolve_url(base):
    """把各种写法的 base_url 归一成 chat/completions 地址"""
    b = (base or DEFAULT_BASE).strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


# ---------------------------------------------------------------- 拼提示词

_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _usable_image(path):
    """只把「真的是图片文件」的交给视觉模型。

    解密失败时曾产出过 .bin 垃圾文件，mimetypes 认不出来、默认按 jpeg 送出去，
    视觉模型直接 4xx。这里按扩展名先筛一道。
    """
    if not path or not os.path.exists(path):
        return False
    return os.path.splitext(path)[1].lower() in _IMG_EXT


def _img_data_url(path):
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_user_content(result, use_vision):
    """把「最近对话 + 她刚发的」拼成一条 user 消息"""
    parts = []

    ctx = result.get("context") or []
    if ctx:
        lines = ["【最近的对话】"]
        for m in ctx:
            who = "她" if m.get("from_her") else "我"
            lines.append(f"{who}：{m.get('text', '')}")
        parts.append("\n".join(lines))

    fresh = []
    has_img = False
    for i in result.get("new_messages") or []:
        t = (i.get("text") or "").strip()
        if not t:
            continue
        if "[图片]" in t:
            has_img = True
        fresh.append(f"她：{t}")
    if fresh:
        parts.append("【她刚发来的消息（需要你现在回复）】\n" + "\n".join(fresh))
    else:
        return None, False

    if has_img:
        if use_vision:
            parts.append("（上面的 [图片] 对应的原图在下面，直接看图里的实际内容自然回应）")
        else:
            parts.append("（她发了图片，但你看不到图；不要承认看不到，按对话自然接话，"
                         "或者问一句「这啥呀」「哪拍的」）")

    text = "\n\n".join(parts)
    if use_vision:
        content = [{"type": "text", "text": text}]
        for img in (result.get("new_images") or [])[:3]:
            p = img.get("path") if isinstance(img, dict) else img
            if _usable_image(p):
                try:
                    content.append({"type": "image_url",
                                    "image_url": {"url": _img_data_url(p)}})
                except OSError:
                    pass
        return content, has_img
    return text, has_img


# ---------------------------------------------------------------- 调接口

def chat(llm, messages):
    """调 chat/completions。

    temperature 各平台取值范围不一样（智谱限制 ≤1，DeepSeek 允许到 2）。
    配置里给了超范围的值就直接 400，所以这里带一次自动降档重试，
    免得换个平台就得改代码。
    """
    url = resolve_url(llm["base_url"])
    temps = [float(llm["temperature"])]
    if temps[0] > 1.0:
        temps.append(0.9)

    last_err = None
    for t in temps:
        try:
            return _post(url, llm, messages, t)
        except RuntimeError as e:
            last_err = e
            # 只有确实是 temperature 引起的才降档重试，别的错直接抛
            if "temperature" not in str(e):
                raise
    raise last_err


def _post(url, llm, messages, temperature):
    body = {
        "model": llm["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": int(llm["max_tokens"]),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (llm.get("api_key") or "")},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=int(llm.get("timeout", 90))) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"返回结构异常: {json.dumps(data, ensure_ascii=False)[:400]}")
    if isinstance(text, list):     # 有些实现返回分段结构
        text = "".join(seg.get("text", "") for seg in text if isinstance(seg, dict))
    return (text or "").strip(), data.get("usage") or {}


# ---------------------------------------------------------------- 清洗输出

# 单条消息的长度上限。提示词要求「2～10 字，超过 12 字就是错的」，
# 这里放宽到 18 作为「模型写跑偏成整段话」的判据；超了必须拆，
# 拆完还长就硬切 —— 宁可拆得生硬，也绝不放一整段过去。
MAX_PIECE = 18


# 模型「复读」的黑名单。视觉模型（实测 glm-4v-flash）有时会不回内容，
# 而是把提示词骨架 / 对方刚发的话原样吐回来，例如
#   "她：[图片]" / "(local_id=710)" / "【输出】"
# 这种行发出去就是一眼假的机器痕迹，必须在最后一道关口拦掉。
ECHO_MARKS = ("【输出】", "【最近的对话】", "【她刚发来的消息",
              "【基本规则】", "【别这么回】", "local_id", "[图片]")


def clean_lines(text, max_lines=3, pet_prefix=""):
    """把模型输出整理成要发的若干条消息。

    pet_prefix：每条开头爱用的称呼（比如"宝宝"）。**由 config.pet_prefix 提供，
    默认是空** —— 以前直接写死成"宝宝"，等于把使用者的说话习惯硬编码进了 exe，
    分发给别人时关都关不掉。想用就在 config.json 里写 "pet_prefix": "宝宝"。
    """
    """把模型输出切成「可以直接发的几条」，去掉序号/引号/括号说明等噪音"""
    out = []
    for raw in (text or "").replace("\r", "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        # 去掉行首序号：1.  1、  -  *  ① 等
        for mark in ("1.", "2.", "3.", "4.", "5.", "1、", "2、", "3、", "1）", "2）", "3）"):
            if s.startswith(mark):
                s = s[len(mark):].strip()
                break
        s = s.lstrip("-*·• ").strip()
        # 去掉整行包裹的引号
        for q in ('"', "'", "「", "」", "“", "”", "『", "』"):
            s = s.strip(q)
        s = s.strip()
        if not s:
            continue
        # 复读提示词骨架 / 对方原话的行直接丢掉（见 ECHO_MARKS 注释）
        if any(m in s for m in ECHO_MARKS) or s.startswith(("她：", "我：")):
            continue
        if len(s) <= MAX_PIECE:
            out.append(s)
            continue
        # 先按标点拆；整句没有标点时拆不出来，就按 MAX_PIECE 硬切。
        # 以前这里「拆出的片段 <= 40 字就照单收下」，结果 19～40 字、没有标点的
        # 长句会被原样当成一条发出去，等于绕过了碎句型这条底线。
        pieces = [f.strip() for f in re.split(r"[，,。！!？?；;、\s]+", s) if f.strip()] or [s]
        for p in pieces:
            if len(p) <= MAX_PIECE:
                out.append(p)
            else:
                out.extend(p[i:i + MAX_PIECE] for i in range(0, len(p), MAX_PIECE))

    pet = (pet_prefix or "").strip()
    if pet:
        # 称呼只允许出现在第一条 —— 小模型经常每条都加，连着喊非常假，这里强制清掉
        forms = (pet + ",", pet + "，", pet + ": ", pet + "：")
        for i in range(1, len(out)):
            t = out[i]
            for p in forms:
                if t.startswith(p):
                    t = t[len(p):].strip()
            out[i] = t.replace(pet, "").strip()
        out = [x for x in out if x]

    # 去掉完全重复的行（小模型爱把"哈哈哈"原样复制三遍，连发三条一样的很怪）
    seen, uniq = set(), []
    for x in out:
        k = x
        if pet:
            k = x.replace(pet + ",", "").replace(pet + "，", "").strip()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    out = uniq[:max_lines]

    # 兜底：第一条正常该带称呼。只有当它本身就是一声纯应答（"哈哈""嗯嗯""好"）
    # 时才省略 —— 真人不会对着一句"哈哈"喊称呼，但"好看""七点见"这种是该带的。
    if pet and out and not any(pet in x for x in out) and not INTERJECTION_RE.match(out[0]):
        out[0] = pet + ", " + out[0]
    return out


# ---------------------------------------------------------------- 对外入口

def has_image(result):
    """是否真的拿到可用的图片文件（不只是消息里带 [图片] 标记）。

    决定要不要切视觉模型：只有图真的解密出来了才切。只要标记、没有文件时
    切过去只是白白用更啰嗦的模型写闲聊。
    """
    for img in (result.get("new_images") or []):
        p = img.get("path") if isinstance(img, dict) else img
        if _usable_image(p):
            return True
    return False


def generate(cfg, result, log=None):
    """根据检测结果生成回复，返回 (lines, meta)。失败时抛 RuntimeError。"""
    llm = llm_cfg(cfg)
    if not llm.get("api_key"):
        raise RuntimeError("没有配置 llm.api_key（config.json 的 llm 段，或环境变量 WECHAT_LLM_KEY）")
    if not llm.get("enabled"):
        raise RuntimeError("config.json 里 llm.enabled 为 false")

    # 只有真的来图了才用视觉模型 —— 视觉模型写闲聊明显更啰嗦、更难守字数限制，
    # 没图就走文本模型。
    has_img = has_image(result)
    use_vision = bool(llm.get("vision_model")) and has_img
    # 视觉模型可能因为图片格式/大小/平台限制直接 4xx。这时候不能让整轮回复卡死 ——
    # 丢掉图片、退回文本模型再来一次，最差也是"看不到图"的自然接话，
    # 而不是一条都发不出去（那会让消息一直留在待回复队列里反复重试）。
    can_downgrade = use_vision

    while True:
        model = llm["vision_model"] if use_vision else llm["model"]
        user_content, _ = build_user_content(result, use_vision)
        if user_content is None:
            return [], {"reason": "没有可回复的文本内容"}
        messages = [{"role": "system", "content": load_prompt()},
                    {"role": "user", "content": user_content}]
        try:
            text, usage = chat(dict(llm, model=model), messages)
        except RuntimeError as e:
            if can_downgrade:
                can_downgrade = False
                use_vision = False
                if log:
                    try:
                        log(f"视觉模型 {model} 调用失败（{e}）→ 丢弃图片改用 {llm['model']} 重试")
                    except Exception:
                        pass
                continue
            raise

        lines = clean_lines(text, int(llm.get("max_lines", 3)),
                            (cfg or {}).get("pet_prefix", ""))
        # 接口没报错，但一条都清不出来 —— 多半是视觉模型把提示词骨架复读了一遍
        # （实测 glm-4v-flash 会输出「她：[图片] / (local_id=710) / 【输出】」）。
        # 这种情况也按失败处理：丢图退回文本模型，最差是自然接话，
        # 而不是把「【输出】」这种东西真发出去。
        if not lines and can_downgrade:
            can_downgrade = False
            use_vision = False
            if log:
                try:
                    log(f"视觉模型 {model} 输出复读（无可发内容）→ 丢弃图片改用 {llm['model']} 重试")
                except Exception:
                    pass
            continue
        break

    meta = {"model": model, "vision": use_vision, "has_image": has_img,
            "usage": usage, "raw": text}
    if log:
        try:
            log(f"LLM {model} 生成 {len(lines)} 条，usage={usage}")
        except Exception:
            pass
    return lines, meta


if __name__ == "__main__":
    # 自测：只拼提示词、不发请求（无需 key）
    import sys
    sys.path.insert(0, HERE)
    import monitor

    cfg = monitor.load_json(os.path.join(HERE, "config.json"), {})
    demo = {
        "context": [
            {"from_her": True, "text": "今天好累"},
            {"from_her": False, "text": "咋了"},
            {"from_her": True, "text": "被领导骂了"},
        ],
        "new_messages": [{"text": "烦死了不想干了"}],
        "new_images": [],
    }
    content, has_img = build_user_content(demo, False)
    out = os.path.join(HERE, "data", "_prompt_preview.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("===== SYSTEM =====\n" + SYSTEM_PROMPT)
        f.write("\n\n===== USER =====\n" + (content if isinstance(content, str) else str(content)))
    print("prompt preview ->", out)
    print("llm cfg:", {k: (v[:6] + "***" if k == "api_key" and v else v)
                       for k, v in llm_cfg(cfg).items()})
