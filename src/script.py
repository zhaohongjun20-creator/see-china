# -*- coding: utf-8 -*-
"""英文讲稿生成（P0-1 修复字段不一致 + 校验 + 有限修复重试）。

两步生成：outline JSON → 逐段扩写到字数。关键改动：
- 对外标准字段统一为 video_query（描述"要搜索的实拍视频"）；
  解析时兼容历史别名（image_query/query/...），但内部结构只有一个名字，
  下游不再各自猜字段。
- 结构化校验：缺 video_query / 空文本 / 段数异常等 HARD 错误会触发
  "修复提示词"有限次重试（默认 2 次）；仍不合法则抛 ScriptValidationError，
  绝不带病进入素材检索阶段。
- 每次调用捕获服务端 usage（有则记录真实 token，无则按字符估算并标注）。
"""
import json
import random
import re

import requests

API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 提示词版本：变化会使 config_fingerprint 改变 → 任务缓存失效、重新生成
PROMPT_VERSION = "seechina-script-v2"

OUTLINE_PROMPT = """You are the head writer of "See China" (带你看中国) — an English-language
YouTube/Instagram series introducing China to international audiences: its landscapes,
food, history, technology, festivals and everyday life.

Topic: {topic}

Write an episode outline as STRICT JSON (no markdown):
{{
 "title": "catchy English title, under 70 chars, may include a number",
 "description": "2-3 sentence English episode description",
 "tags": ["6 english youtube tags"],
 "sections": [
 {{"text": "", "video_query": "3-6 english words for searching STOCK FOOTAGE, e.g. 'great wall aerial sunrise'"}}
 ]}}
Rules:
- exactly {n} sections
- narration in vivid, friendly spoken English (like a travel host)
- video_query must be VISUAL and filmable (no abstract words), unique per section,
  and must describe footage that matches that section's narration
- facts must be accurate and balanced; no politics, no stereotypes
Return JSON only."""

EXPAND_PROMPT = """You write narration for "See China" (English series for foreign audiences).
Topic: {topic}
Section {i}/{n}. Write the narration for this section in {lo}-{hi} words.
Requirement: vivid spoken English, concrete facts/images, natural transitions, no politics.
Existing title context: {title}
Output ONLY the narration text (no headings, no quotes)."""

FIX_PROMPT = """The following JSON outline for "See China" has problems that MUST be fixed:
{problems}

Broken outline:
{outline}

Return the corrected outline as STRICT JSON with the SAME structure:
title / description / tags / sections[] where EVERY section has non-empty "text"
and non-empty "video_query" (3-6 visual english words for stock-footage search).
Keep the same topic and section count. Return JSON only, no markdown."""

# video_query 的兼容别名（按优先级）；内部统一输出 video_query
_VQ_ALIASES = ("video_query", "image_query", "query", "search_query",
               "footage_query", "video_keyword", "video", "footage", "keywords")


class ScriptError(RuntimeError):
    """讲稿生成/校验失败；problems 携带结构化问题清单。"""

    def __init__(self, message: str, problems: list | None = None):
        self.problems = problems or []
        super().__init__(message)


class _Usage:
    def __init__(self):
        self.real_tokens = 0        # 服务端返回的真实 token（可信）
        self.estimated = False      # 只能字符估算时置 True
        self.chars_in = 0
        self.chars_out = 0
        self.calls = 0

    def add(self, usage: dict | None, prompt: str, completion: str):
        self.calls += 1
        self.chars_in += len(prompt)
        self.chars_out += len(completion)
        if usage and usage.get("total_tokens"):
            self.real_tokens += int(usage.get("total_tokens") or 0)
        else:
            self.estimated = True

    def as_dict(self) -> dict:
        return {"calls": self.calls, "real_tokens": self.real_tokens,
                "chars_in": self.chars_in, "chars_out": self.chars_out,
                "tokens_estimated": self.estimated}


def _call(api_key: str, model: str, prompt: str, temperature: float,
          usage: _Usage) -> str:
    try:
        r = requests.post(API, headers={"Authorization": f"Bearer {api_key}"},
                          json={"model": model, "temperature": temperature,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=120)
    except requests.RequestException as e:
        raise ScriptError(f"script LLM network error: {type(e).__name__}: {e}") from e
    if r.status_code in (401, 403):
        raise ScriptError("script LLM auth failed (401/403): check ZHIPU_API_KEY")
    if r.status_code == 429:
        raise ScriptError("script LLM rate limited (429): retry later")
    r.raise_for_status()
    body = r.json()
    text = body["choices"][0]["message"]["content"]
    usage.add(body.get("usage"), prompt, text)
    return text


def _json_of(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        raise ScriptError(f"model returned invalid JSON: {e}; head={text[:120]!r}")


# ── 规范化：把模型各种返回习惯 → 内部统一结构 ─────────────
def _norm_query(v) -> str:
    """取材词规范化：字符串直接清洗；列表取前几个词拼接；过长截断到 6 词。"""
    if isinstance(v, (list, tuple)):
        v = " ".join(str(x) for x in v if x)
    v = re.sub(r"\s+", " ", str(v)).strip().strip('"\'').strip()
    words = v.split(" ")
    return " ".join(words[:6]) if len(words) > 6 else v


def _pick_vq(sec: dict) -> str:
    for key in _VQ_ALIASES:
        if key in sec:
            got = _norm_query(sec[key])
            if got:
                return got
    return ""


def normalize_outline(raw: dict) -> dict:
    """任意模型返回 → 内部标准结构 {title, description, tags, sections[{text, video_query}]}。"""
    if not isinstance(raw, dict):
        raise ScriptError("outline is not a JSON object")
    secs_in = raw.get("sections")
    if not isinstance(secs_in, list):
        secs_in = []
    sections = []
    for sec in secs_in:
        if not isinstance(sec, dict):
            sec = {"text": str(sec)}
        text = re.sub(r"\s+", " ", str(sec.get("text") or "")).strip()
        sections.append({"text": text, "video_query": _pick_vq(sec)})
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,;]", tags) if t.strip()]
    return {"title": str(raw.get("title") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "tags": [str(t).strip() for t in tags if str(t).strip()][:8],
            "sections": sections}


# ── 校验：hard 错误可触发修复重试；warning 只记录 ──────────
def validate(doc: dict, n_min: int, n_max: int, c_lo: int, c_hi: int,
             require_text: bool = True) -> list:
    """返回问题列表 [{level: hard|warn, section, problem}]；存在 hard 即不合格。

    require_text=False 用于大纲阶段（文本由逐段扩写补齐，此时只校验结构
    和 video_query）；扩写完成后用默认值做最终校验。
    """
    problems = []
    if not doc.get("title"):
        problems.append({"level": "hard", "section": 0, "problem": "missing title"})
    elif len(doc["title"]) > 80:
        problems.append({"level": "hard", "section": 0,
                         "problem": f"title too long ({len(doc['title'])} chars > 80)"})
    elif len(doc["title"]) > 70:
        problems.append({"level": "warn", "section": 0,
                         "problem": f"title slightly long ({len(doc['title'])} > 70 chars)"})
    secs = doc.get("sections") or []
    if len(secs) < 3:
        problems.append({"level": "hard", "section": 0,
                         "problem": f"only {len(secs)} sections (<3)"})
    elif not (n_min <= len(secs) <= n_max):
        problems.append({"level": "warn", "section": 0,
                         "problem": f"{len(secs)} sections outside [{n_min},{n_max}]"})
    if not doc.get("tags"):
        problems.append({"level": "warn", "section": 0, "problem": "no tags"})
    for i, sec in enumerate(secs):
        words = len(sec.get("text", "").split())
        if require_text:
            if words < 15:
                problems.append({"level": "hard", "section": i,
                                 "problem": f"text too short ({words} words < 15)"})
            elif not (c_lo <= words <= c_hi):
                problems.append({"level": "warn", "section": i,
                                 "problem": f"text {words} words outside [{c_lo},{c_hi}]"})
        if not sec.get("video_query"):
            problems.append({"level": "hard", "section": i,
                             "problem": "missing video_query (footage keyword)"})
    return problems


def _problems_text(problems: list) -> str:
    hard = [p for p in problems if p["level"] == "hard"]
    warn = [p for p in problems if p["level"] == "warn"]
    lines = ["HARD (must fix):"] + [
        f"  - section {p['section']}: {p['problem']}" for p in hard] or ["  (none)"]
    if warn:
        lines += ["WARN (ok to keep):"] + [
            f"  - section {p['section']}: {p['problem']}" for p in warn]
    return "\n".join(lines)


def generate(api_key: str, topic: str, model: str = "glm-4-flash",
             temperature: float = 0.7, n_min: int = 10, n_max: int = 14,
             c_lo: int = 55, c_hi: int = 95, max_fix_attempts: int = 2) -> dict:
    """两步生成 + 规范化 + 校验。hard 问题触发修复重试；仍失败抛 ScriptError。

    返回 doc：{title, description, tags, sections[{text, video_query}],
              _usage, _prompt_version}
    """
    usage = _Usage()
    n = random.randint(n_min, n_max)
    raw = _json_of(_call(api_key, model,
                         OUTLINE_PROMPT.format(topic=topic, n=n), temperature, usage))
    doc = normalize_outline(raw)

    # 大纲阶段只校验结构 + video_query（文本由下面的扩写补齐）
    for attempt in range(max_fix_attempts + 1):
        problems = validate(doc, n_min, n_max, c_lo, c_hi, require_text=False)
        hard = [p for p in problems if p["level"] == "hard"]
        if not hard:
            break
        if attempt == max_fix_attempts:
            raise ScriptError(
                f"script failed validation after {max_fix_attempts} fix attempts",
                problems)
        fixed = _json_of(_call(
            api_key, model,
            FIX_PROMPT.format(problems=_problems_text(problems),
                              outline=json.dumps(doc)[:6000]),
            temperature, usage))
        doc = normalize_outline(fixed)
    else:  # pragma: no cover
        raise ScriptError("script validation loop exited unexpectedly")

    # 逐段扩写（outline 里 text 为空或过短都重新扩写——模型常在大纲里写
    # 十来个词的提纲式 text，非空但低于 15 词会卡死最终校验）
    for i, sec in enumerate(doc["sections"], 1):
        if len(sec["text"].split()) < 15:
            sec["text"] = _call(api_key, model,
                                EXPAND_PROMPT.format(topic=topic, i=i,
                                                     n=len(doc["sections"]),
                                                     lo=c_lo, hi=c_hi,
                                                     title=doc.get("title", "")),
                                temperature, usage).strip()

    # 扩写可能引入新问题（如过短），最终再校验一次（含文本要求）
    problems = validate(doc, n_min, n_max, c_lo, c_hi, require_text=True)
    hard = [p for p in problems if p["level"] == "hard"]
    if hard:
        detail = "; ".join(f"section {p['section']}: {p['problem']}"
                           for p in hard)
        raise ScriptError(
            f"script failed final validation after expansion: {detail}",
            problems)
    doc["_warnings"] = [p["problem"] for p in problems if p["level"] == "warn"]

    doc["_usage"] = usage.as_dict()
    doc["_prompt_version"] = PROMPT_VERSION
    return doc
