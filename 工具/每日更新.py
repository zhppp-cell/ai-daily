#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统训练台 · 每日更新脚本

在 GitHub Actions 里跑：抓 AI 能力变化 → 生成当天的 30 分钟学习单元 →
重写 index.html → 更新进度 → 提交推送。

本地调试：python 工具/每日更新.py --no-push
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS_FILE = os.path.join(ROOT, "进度.json")
TEMPLATE_FILE = os.path.join(ROOT, "工具", "模板.html")
DATA_DIR = os.path.join(ROOT, "数据")
FEEDBACK_FILE = os.path.join(ROOT, "反馈.md")
UA = "ai-daily-bot/1.0 (+https://github.com/zhppp-cell/ai-daily)"

AI_KEYWORDS = re.compile(
    r"(?i)\b(ai|llm|gpt|claude|gemini|llama|qwen|deepseek|model|agent|openai|anthropic|"
    r"deepmind|neural|inference|gpu|transformer|rag|diffusion|robotics|multimodal)\b"
)

# 链条图层：前两条已写好，后面的链条在启用时由模型生成
CHAIN_LIBRARY = {
    "位置精度链": [
        "指令从哪来：上位机 / PLC 下发的目标位置",
        "指令怎么传：总线（EtherCAT）到驱动器",
        "驱动器怎么执行：位置环 → 速度环 → 电流环 → PWM",
        "电机怎么转：转矩 → 转子 → 传动机构",
        "机械怎么到位：刚度、间隙、摩擦、预紧",
        "位置怎么被测：编码器 / 光栅尺 → 分辨率 → 反馈",
        "误差怎么成指标：定位精度、重复定位精度、反向差值、稳定性",
    ],
    "GitHub 协作链": [
        "提交与历史：工作是怎么被一条条记录下来的，为什么能回退比记得住重要",
        "分支与同步：为什么会「没拉就先推不上去」",
        "权限与密钥：Token 与 scope，为什么有些操作做不了",
        "Issue：把要做的事变成可追踪、可关闭的条目",
        "Pull Request 与 Review：别人（含 AI）改你的文件，你审差异再合并",
        "Actions：让机器替你在云端定时干活",
        "和 AI 配合的三条铁律：先开分支 / 改完看差异 / 随时能退回",
    ],
}


# ---------------------------------------------------------------- 工具函数

def log(*args):
    print(*args, flush=True)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def http_text(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, data=None, headers=None, timeout=30):
    return json.loads(http_text(url, data=data, headers=headers, timeout=timeout))


def parse_json_loose(text):
    """从模型输出里抠出第一个完整的 JSON 对象或数组。"""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    starts = []
    for opener, closer in (("{", "}"), ("[", "]")):
        idx = text.find(opener)
        if idx >= 0:
            starts.append((idx, opener, closer))
    starts.sort()  # 谁在前面就先按谁解析（数组在前就整体解析数组）
    for start, opener, closer in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


# ---------------------------------------------------------------- 抓取

def fetch_hn(limit=40, want=10, budget_seconds=90):
    """Hacker News 热榜里筛 AI 相关，按热度排序。"""
    import time
    deadline = time.time() + budget_seconds
    try:
        ids = http_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:limit]
    except Exception as e:
        log("HN 列表抓取失败:", e)
        return []
    items = []
    for i in ids[:limit]:
        try:
            it = http_json("https://hacker-news.firebaseio.com/v0/item/%s.json" % i, timeout=12)
        except Exception:
            continue
        title = (it or {}).get("title") or ""
        if not AI_KEYWORDS.search(title):
            continue
        items.append({
            "t": title,
            "url": it.get("url") or "https://news.ycombinator.com/item?id=%s" % i,
            "score": it.get("score") or 0,
        })
        if len(items) >= want:
            break
        if time.time() > deadline:
            log("HN 抓取到时间上限，先停")
            break
    items.sort(key=lambda x: -x["score"])
    return items[:8]


def fetch_techcrunch(limit=8):
    try:
        xml = http_text("https://techcrunch.com/category/artificial-intelligence/feed/")
    except Exception as e:
        log("TechCrunch 抓取失败:", e)
        return []
    out = []
    for block in re.findall(r"(?s)<item>.*?</item>", xml)[:limit]:
        t = re.search(r"(?s)<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block)
        l = re.search(r"(?s)<link>(.*?)</link>", block)
        d = re.search(r"(?s)<pubDate>(.*?)</pubDate>", block)
        if t and l:
            out.append({
                "t": re.sub(r"\s+", " ", t.group(1)).strip(),
                "url": l.group(1).strip(),
                "date": (d.group(1) if d else "")[:16],
            })
    return out


# ---------------------------------------------------------------- 模型调用

def call_model(messages, max_tokens=1400):
    """优先用 GitHub 自带模型（Actions 里自带 GITHUB_TOKEN），
    其次用 DeepSeek（需要 DEEPSEEK_API_KEY），都失败返回 None。"""
    errors = []

    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        try:
            r = http_json(
                "https://models.github.ai/inference/chat/completions",
                data={
                    "model": os.environ.get("MODEL_NAME", "openai/gpt-4o-mini"),
                    "messages": messages,
                    "temperature": 0.6,
                    "max_tokens": max_tokens,
                },
                headers={"Authorization": "Bearer %s" % gh_token, "Accept": "application/json"},
                timeout=90,
            )
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            errors.append("github-models: %s" % e)

    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    if ds_key:
        try:
            r = http_json(
                "https://api.deepseek.com/chat/completions",
                data={
                    "model": os.environ.get("MODEL_NAME", "deepseek-chat"),
                    "messages": messages,
                    "temperature": 0.6,
                    "max_tokens": max_tokens,
                },
                headers={"Authorization": "Bearer %s" % ds_key},
                timeout=120,
            )
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            errors.append("deepseek: %s" % e)

    if errors:
        log("模型调用都失败了:", "; ".join(errors))
    else:
        log("没有可用的模型通道（既没有 GITHUB_TOKEN 也没有 DEEPSEEK_API_KEY）")
    return None


PROFILE = (
    "学生背景：机械本科毕业，现在是光刻机部件「待测镜调台」的测试工程师，"
    "日常接触转台/调台的定位精度与重复定位精度测试、最小步进、模态测试（LMS）、"
    "FOC 无刷伺服、Codesys/ST 程序、热耗数据。"
    "他的目标是系统工程师式的横向宽度：每一层只要懂到「能判断、能提问、能验收」，"
    "不要求自己推导公式或写完整程序。每天只有 30 分钟。"
)


def gen_task(progress, chain_name, day, layer):
    prompt = (
        PROFILE
        + "\n\n今天的学习单元：第 %d 条链「%s」的第 %d 层：%s"
        % (progress["current"]["chain_index"], chain_name, day, layer)
        + "\n\n请设计一个 20 分钟能完成的单元。要求：用他能懂的话写，术语第一次出现时用一句话解释；"
        "必须绑到他的真实场景（调台、重复定位精度、最小步进、模态测试、伺服、Codesys、热耗数据）；"
        "act 这一段要给出他今天就能在自己数据或设备上做的一件小事；不要空话。"
        "\n只输出 JSON，不要任何多余文字，格式："
        '{"title":"...","problem":"...","metrics":["3-4 条"],"fail":["3-4 条，写成「现象 → 先怀疑什么」"],'
        '"ask":["2-3 条，他会问专家/供应商的问题"],"act":"...","goal":"一句话过关标准"}'
    )
    out = call_model([{"role": "user", "content": prompt}])
    data = parse_json_loose(out)
    if not isinstance(data, dict):
        return None
    if not all(data.get(k) for k in ("title", "problem", "act", "goal")):
        return None
    for k in ("metrics", "fail", "ask"):
        if not isinstance(data.get(k), list) or not data[k]:
            return None
    return data


def gen_ai_items(candidates):
    lines = []
    for c in candidates:
        lines.append("- %s | %s | %s" % (c.get("t"), c.get("url"), c.get("score") or c.get("date", "")))
    prompt = (
        PROFILE
        + "\n\n下面是今天抓到的 AI 动态候选（标题 | 链接 | 热度或日期）：\n"
        + "\n".join(lines)
        + "\n\n请挑 1 到 2 条属于「能力变化」的（新模型、新工具、能力边界变化）。"
        "不要选融资、产品促销、榜单、会议广告。"
        "\n只输出 JSON 数组，不要多余文字，格式："
        '[{"t":"中文标题","url":"原链接","d":"一句话说明","why":"这对一个测试工程师意味着什么，尽量挂到他正在学的链条上"}]'
    )
    out = call_model([{"role": "user", "content": prompt}], max_tokens=800)
    data = parse_json_loose(out)
    if isinstance(data, dict):
        data = data.get("items") or data.get("ai") or []
    if not isinstance(data, list) or not data:
        return None
    clean = []
    for it in data[:2]:
        if isinstance(it, dict) and it.get("t") and it.get("url"):
            clean.append({
                "t": str(it.get("t")),
                "url": str(it.get("url")),
                "d": str(it.get("d") or ""),
                "why": str(it.get("why") or ""),
            })
    return clean or None


def gen_layers(chain_name):
    prompt = (
        PROFILE
        + "\n\n请把「%s」这条链拆成 7 层，从系统输入端到最终结果，每层一句话，"
        "顺序按「指令/输入 → 中间环节 → 测量 → 指标/结论」推进。" % chain_name
        + '\n只输出 JSON 数组，7 个字符串，不要多余文字。例如：["第1层...","第2层..."]'
    )
    out = call_model([{"role": "user", "content": prompt}], max_tokens=700)
    data = parse_json_loose(out)
    if isinstance(data, dict):
        data = data.get("layers") or []
    if isinstance(data, list) and len(data) == 7 and all(isinstance(x, str) for x in data):
        return data
    return None


# ---------------------------------------------------------------- 兜底内容

def fallback_task(chain_name, day, layer):
    return {
        "title": "%s · 第 %d 层：%s" % (chain_name, day, layer),
        "problem": "今天模型通道没跑通，只给出这一层的位置和问题范围，供你先建立印象。",
        "metrics": [
            "这一层在整条链里负责什么，输入是什么、输出是什么",
            "它最常引起什么现象（精度差、重复性差、还是漂移）",
            "如果它出问题，你在数据上会先看到什么",
        ],
        "fail": [
            "数据整体偏移 → 先看这一层之前的基准或标定",
            "重复性突然变差 → 先看这一层有没有松动、温漂或噪声",
            "现象只在某个方向出现 → 先怀疑这一层有方向相关的间隙或摩擦",
        ],
        "ask": [
            "这一层的规格书/参数表在哪？关键指标是多少？",
            "这一层出过什么现场问题，最后是怎么定位的？",
        ],
        "act": "把这一层的名字写下来，然后在你手上的设备或数据里找出一个能对应上的具体物件或读数。",
        "goal": "能用一句话说清这一层在链条里的作用，以及它最可能带来的误差现象。",
    }


def fallback_ai(candidates):
    out = []
    for c in candidates[:2]:
        out.append({
            "t": c.get("t", ""),
            "url": c.get("url", ""),
            "d": "今天模型通道没跑通，只保留条目，说明暂缺。",
            "why": "",
        })
    if not out:
        out.append({
            "t": "今天没有抓到合适的 AI 动态",
            "url": "https://news.ycombinator.com/",
            "d": "抓取源今天没有返回可用条目。",
            "why": "",
        })
    return out


# ---------------------------------------------------------------- 主流程

def read_feedback():
    if not os.path.exists(FEEDBACK_FILE):
        return None
    with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip() and not ln.lstrip().startswith(("#", ">", "<!--"))]
    text = "\n".join(lines).strip()
    if not text:
        return None
    return text


def advance(progress, chain_name, layers):
    cur = progress["current"]
    cur["layers"] = layers
    if cur["day"] < len(layers):
        cur["day"] += 1
        return
    progress.setdefault("done", []).append(chain_name)
    roadmap = progress.get("chain_roadmap", [])
    nxt = cur["chain_index"]  # 0-based index of next chain
    if nxt >= len(roadmap):
        cur["day"] = len(layers)  # 走到头就停在这一层
        return
    cur["chain_index"] = nxt + 1
    cur["chain_name"] = roadmap[nxt]
    cur["day"] = 1
    cur["layers"] = CHAIN_LIBRARY.get(cur["chain_name"]) or gen_layers(cur["chain_name"]) or [
        "（待补）这条链的图层还没生成，明天会自动补上"
    ] * 1


def git(args, check=True):
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True, text=True)
    if r.stdout:
        log("git", " ".join(args), "->", r.stdout.strip())
    if r.returncode != 0 and check:
        log("git 失败:", r.stderr.strip())
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只生成文件，不提交推送")
    ap.add_argument("--date", default=None, help="覆盖日期，格式 YYYY-MM-DD")
    args = ap.parse_args()

    today = args.date or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    weekday = "周" + "一二三四五六日"[datetime.date.fromisoformat(today).weekday()]

    progress = load_json(PROGRESS_FILE)
    if not progress:
        raise SystemExit("缺少 进度.json")
    cur = progress["current"]
    chain_name, day = cur["chain_name"], cur["day"]
    layers = cur.get("layers") or CHAIN_LIBRARY.get(chain_name) or []
    if not layers:
        layers = gen_layers(chain_name) or ["（待补）"] * 7
        cur["layers"] = layers
    layer = layers[min(day, len(layers)) - 1]

    log("今天:", today, "| 链条:", chain_name, "| 第", day, "层:", layer)

    candidates = fetch_hn() + fetch_techcrunch()
    log("抓到候选条目:", len(candidates))

    task = gen_task(progress, chain_name, day, layer) or fallback_task(chain_name, day, layer)
    ai_items = gen_ai_items(candidates) or fallback_ai(candidates)

    feedback = read_feedback()
    yesterday = feedback or "还没有反馈。做完今天这一步，把一句话判断发给我或写进 反馈.md，明天会显示在这里。"

    data = {
        "date": "%s %s" % (today, weekday),
        "chain": {"name": chain_name, "day": day, "layers": layers},
        "task": task,
        "ai": ai_items,
        "yesterday": yesterday,
    }

    save_json(os.path.join(DATA_DIR, "%s.json" % today), data)

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    html = tpl.replace("__DATA__", payload)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    log("已生成 index.html")

    advance(progress, chain_name, layers)
    progress["updated"] = today
    save_json(PROGRESS_FILE, progress)

    if feedback:
        with open(FEEDBACK_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write("")

    if args.no_push:
        log("--no-push：跳过提交推送")
        return

    git(["config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "ai-daily-bot")])
    git(["config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "ai-daily-bot@users.noreply.github.com")])
    git(["add", "-A"])
    git(["commit", "-m", "自动更新 %s：%s 第 %d 层" % (today, chain_name, day)], check=False)
    for i in range(1, 7):
        if git(["push"], check=False):
            log("推送成功")
            return
        log("第 %d 次推送失败，重试" % i)
    raise SystemExit("推送连续失败")


if __name__ == "__main__":
    main()
