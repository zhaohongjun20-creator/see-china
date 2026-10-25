# -*- coding: utf-8 -*-
"""内容审核（P1-4）：分层审核，关键异常禁止自动放行。

三层：
  1. 确定性检查 deterministic_issues() —— 不花 token：段落重复、占位符残留、
     英文产线混入中文、段落过短等。客观问题直接 flagged。
  2. AI 内容审核 ai_review() —— 事实错误 / 重复 / 逻辑断裂 / 旁白与取材词
     不匹配 / 标题夸大 / 政治敏感。模型输出结构化问题清单（问题+段落+严重度
     +建议+是否需要来源），而不是只返回一个 pass。
  3. 人工审核入口 —— needs_source 或 high 级问题 → 决策 "human"，
     CLI `python autopost.py review-job <job_id> --approve/--reject` 裁决。

铁律：审核服务超时 / 返回非法 JSON / 任何异常 → status="unavailable"，
decide() 返回 "retry" 等待重试 —— 绝不视为审核通过（对应 P1-4 验收标准）。
"""
import json
import re

import requests

from .script import PROMPT_VERSION  # noqa: F401  (保持提示词版本可追溯)

REVIEW_PROMPT = """You are the fact-review editor for "See China", an English YouTube
series introducing China to international audiences.

Review this episode outline. Look for:
- factual errors or dubious numbers/claims
- duplicated or repeated narration between sections
- broken logic or abrupt jumps between sections
- narration that clearly does not match its stock-footage keyword
- exaggerated clickbait claims in the title
- politically sensitive or stereotyping content

Episode title: {title}
Footage keywords and narration:
{sections}

Return STRICT JSON (no markdown):
{{"verdict": "pass" or "flagged",
 "issues": [{{"section": <0-based section index, -1 for title>,
   "severity": "high"|"medium"|"low",
   "issue": "what is wrong",
   "suggestion": "how to fix",
   "needs_source": true|false}}]}}

Rules:
- only report real problems, no style nitpicks
- severity "high" = factually wrong / must fix before publishing
- needs_source=true when a key fact, number or quote has no verifiable source
- if everything is fine, return {{"verdict": "pass", "issues": []}}
Return JSON only."""

API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 审核通道允许"降级放行"的非关键故障（目前为空 —— 默认一切异常都不放行）。
# 如确有需要，把可容忍的错误子串加进来并在 code review 里说明理由。
NON_CRITICAL_FAILURES = ()


def _llm_json(api_key: str, model: str, prompt: str, temperature: float):
    """调用 LLM 并解析 JSON。任何失败抛 ReviewUnavailable。"""
    try:
        r = requests.post(API, headers={"Authorization": f"Bearer {api_key}"},
                          json={"model": model, "temperature": temperature,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=120)
    except requests.RequestException as e:
        raise ReviewUnavailable(f"network: {type(e).__name__}: {e}") from e
    if r.status_code in (401, 403):
        raise ReviewUnavailable(f"auth failed http {r.status_code}")
    if r.status_code == 429:
        raise ReviewUnavailable("rate limited (429)")
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    try:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        raise ReviewUnavailable(f"invalid JSON from review model: {e}; "
                                f"head={text[:120]!r}") from e


class ReviewUnavailable(Exception):
    """审核服务不可用 —— 上游必须按"未过审"处理，不得自动放行。"""


# ── 第一层：确定性检查（不花 token） ─────────────────────
_PLACEHOLDER = re.compile(r"TODO|FIXME|XXX|lorem ipsum|placeholder", re.I)


def deterministic_issues(doc: dict) -> list:
    issues = []
    secs = doc.get("sections") or []
    seen = {}
    for i, sec in enumerate(secs):
        text = re.sub(r"\s+", " ", sec.get("text", "")).strip().lower()
        if not text:
            issues.append({"section": i, "severity": "high",
                           "issue": "empty narration text",
                           "suggestion": "regenerate script",
                           "needs_source": False})
            continue
        if _PLACEHOLDER.search(sec["text"]):
            issues.append({"section": i, "severity": "high",
                           "issue": "placeholder/debug residue in narration",
                           "suggestion": "remove and rewrite",
                           "needs_source": False})
        if len(sec["text"]) >= 100:
            cjk = sum(1 for ch in sec["text"] if "\u4e00" <= ch <= "\u9fff")
            if cjk / len(sec["text"]) > 0.15:
                issues.append({"section": i, "severity": "high",
                               "issue": "Chinese text in English narration",
                               "suggestion": "rewrite in English",
                               "needs_source": False})
        if text in seen:
            issues.append({"section": i, "severity": "high",
                           "issue": f"duplicates section {seen[text]} (repeated narration)",
                           "suggestion": "rewrite to differentiate",
                           "needs_source": False})
        else:
            seen[text] = i
        if not sec.get("video_query"):
            issues.append({"section": i, "severity": "high",
                           "issue": "missing video_query",
                           "suggestion": "add footage keyword",
                           "needs_source": False})
    return issues


# ── 第二层：AI 内容审核 ─────────────────────────────────
def ai_review(api_key: str, doc: dict, model: str = "glm-4-flash",
              temperature: float = 0.2) -> dict:
    """返回 {status: pass|flagged|unavailable, issues: [...], raw_verdict}。

    status=unavailable 表示审核通道故障 —— 调用方必须当作未过审处理。
    """
    secs = "\n".join(
        f"[{i}] query={s.get('video_query', '')!r}\n{s.get('text', '')}"
        for i, s in enumerate(doc.get("sections") or []))
    prompt = REVIEW_PROMPT.format(title=doc.get("title", ""), sections=secs)
    try:
        data = _llm_json(api_key, model, prompt, temperature)
    except ReviewUnavailable as e:
        if any(s in str(e) for s in NON_CRITICAL_FAILURES):
            pass  # 明确允许降级的非关键故障（当前为空）
        else:
            return {"status": "unavailable",
                    "issues": [{"section": -1, "severity": "high",
                                "issue": f"review channel unavailable: {e}",
                                "suggestion": "retry later; do NOT publish unchecked",
                                "needs_source": False}],
                    "raw_verdict": None}
    issues = data.get("issues") or []
    clean = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        clean.append({"section": int(it.get("section", -1)),
                      "severity": str(it.get("severity", "medium")).lower(),
                      "issue": str(it.get("issue", "")),
                      "suggestion": str(it.get("suggestion", "")),
                      "needs_source": bool(it.get("needs_source"))})
    verdict = str(data.get("verdict", "")).lower()
    status = "pass" if verdict == "pass" and not clean else "flagged"
    return {"status": status, "issues": clean, "raw_verdict": verdict or None}


def run_review(api_key: str, doc: dict, model: str = "glm-4-flash") -> dict:
    """确定性检查 + AI 审核合并。确定性 high 问题直接 flagged（省 token）。"""
    det = deterministic_issues(doc)
    det_high = [i for i in det if i["severity"] == "high"]
    result = ai_review(api_key, doc, model)
    result["deterministic_issues"] = det
    if det_high:
        # 客观已坏的内容不需要 AI 复核结论来定性
        result["status"] = "flagged"
        result["issues"] = det_high + result["issues"]
    return result


def decide(result: dict) -> str:
    """审核结果 → 管线决策：
       ready  —— 通过，继续打包
       human  —— 有 high 级或 needs_source 问题，等人工裁决
       retry  —— 审核通道不可用，保留文件等待重试（绝不放行）
    """
    if result["status"] == "unavailable":
        return "retry"
    blocking = [i for i in result["issues"]
                if i.get("severity") == "high" or i.get("needs_source")]
    if result["status"] == "flagged" and blocking:
        return "human"
    return "ready"


def decide_with_flags(result: dict, sapi_used: bool = False,
                      fallback_used: bool = False) -> str:
    """在 decide() 基础上叠加任务级人工审核标记（评审#3/#6）。

    - sapi_used：使用了本地 SAPI 机械音兜底 → 强制人工裁决；
    - fallback_used：有段落用了通用兜底素材（china city scenery）→ 强制人工裁决，
      不允许"旁白讲熊猫、画面放城市风景"的成片仅凭 AI pass 就进发布包。
    审核通道不可用时优先 retry —— 等恢复后连同标记一起重新裁决。
    """
    flags = []
    if sapi_used:
        flags.append(("narration produced with local SAPI fallback "
                      "(mechanical voice, quality gate)",
                      "re-synthesize with a neural voice, or approve manually"))
    if fallback_used:
        flags.append(("generic fallback footage (china city scenery) used for "
                      "some sections — visuals may not match narration",
                      "replace with topic-specific footage or approve manually"))
    if flags and result.get("status") != "unavailable":
        result = dict(result)
        result["status"] = "flagged"
        issues = list(result.get("issues", []))
        for issue, suggestion in flags:
            issues.append({"section": -1, "severity": "high", "issue": issue,
                           "suggestion": suggestion, "needs_source": False})
        result["issues"] = issues
    return decide(result)
