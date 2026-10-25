# -*- coding: utf-8 -*-
"""英文讲稿生成（两步：outline JSON → 逐段扩写，字数达标）。"""
import json, random, re, requests

API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

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
- video_query must be VISUAL and filmable (no abstract words), unique per section
- facts must be accurate and balanced; no politics, no stereotypes
Return JSON only."""

EXPAND_PROMPT = """You write narration for "See China" (English series for foreign audiences).
Topic: {topic}
Section {i}/{n}. Write the narration for this section in {lo}-{hi} words.
Requirement: vivid spoken English, concrete facts/images, natural transitions, no politics.
Existing title context: {title}
Output ONLY the narration text (no headings, no quotes)."""


def _call(api_key: str, model: str, prompt: str, temperature: float) -> str:
    r = requests.post(API, headers={"Authorization": f"Bearer {api_key}"},
                      json={"model": model, "temperature": temperature,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _json_of(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def generate(api_key: str, topic: str, model: str = "glm-4-flash",
             temperature: float = 0.7, n_min: int = 10, n_max: int = 14,
             c_lo: int = 55, c_hi: int = 95) -> dict:
    """两步生成：大纲（含每段取材词）→ 逐段扩写到字数。"""
    n = random.randint(n_min, n_max)
    outline = _json_of(_call(api_key, model,
                             OUTLINE_PROMPT.format(topic=topic, n=n), temperature))
    secs = outline.get("sections") or []
    for i, sec in enumerate(secs, 1):
        if not sec.get("text"):
            sec["text"] = _call(api_key, model,
                                EXPAND_PROMPT.format(topic=topic, i=i, n=len(secs),
                                                     lo=c_lo, hi=c_hi,
                                                     title=outline.get("title", "")),
                                temperature).strip()
    outline.setdefault("description", "")
    outline.setdefault("tags", [])
    return outline
