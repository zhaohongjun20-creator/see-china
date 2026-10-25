# -*- coding: utf-8 -*-
import pytest

from src import review as R


def _doc(sections=2):
    # 每段文本必须不同，否则触发“段落重复”确定性检查
    return {"title": "A Test Episode",
            "sections": [{"text": f"word {i} " * 30, "video_query": f"q{i}"}
                         for i in range(sections)]}


# ── 第一层：确定性检查 ──────────────────────────────────
def test_deterministic_chinese_in_english():
    doc = _doc()
    doc["sections"][0]["text"] = "这是一段中文旁白，" * 20
    issues = R.deterministic_issues(doc)
    assert any("Chinese text" in i["issue"] for i in issues)


def test_deterministic_duplicate_sections():
    doc = _doc()
    doc["sections"][1]["text"] = doc["sections"][0]["text"]
    issues = R.deterministic_issues(doc)
    assert any("duplicates" in i["issue"] and i["severity"] == "high"
               for i in issues)


def test_deterministic_placeholder_and_missing_query():
    doc = _doc()
    doc["sections"][0]["text"] = "TODO fill this section later with words"
    doc["sections"][1]["video_query"] = ""
    issues = R.deterministic_issues(doc)
    assert any("placeholder" in i["issue"] for i in issues)
    assert any("video_query" in i["issue"] for i in issues)


def test_deterministic_clean_doc():
    assert R.deterministic_issues(_doc()) == []


# ── 第二层：AI 审核（异常必须转 unavailable，绝不放行） ────
def test_ai_pass(monkeypatch):
    monkeypatch.setattr(R, "_llm_json",
                        lambda *a, **k: {"verdict": "pass", "issues": []})
    assert R.ai_review("k", _doc())["status"] == "pass"


def test_ai_flagged_with_issues(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: {
        "verdict": "flagged",
        "issues": [{"section": 0, "severity": "high", "issue": "wrong year",
                    "suggestion": "fix", "needs_source": False}]})
    res = R.ai_review("k", _doc())
    assert res["status"] == "flagged"
    assert res["issues"][0]["severity"] == "high"


def test_ai_unavailable_on_any_failure(monkeypatch):
    monkeypatch.setattr(R, "_llm_json",
                        lambda *a, **k: (_ for _ in ()).throw(
                            R.ReviewUnavailable("timeout after 120s")))
    res = R.ai_review("k", _doc())
    assert res["status"] == "unavailable"       # 不允许降级为 pass


def test_ai_malformed_issues_filtered(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: {
        "verdict": "flagged", "issues": ["garbage", 42,
                                         {"severity": "low", "issue": "ok"}]})
    res = R.ai_review("k", _doc())
    assert len(res["issues"]) == 1              # 非法条目被剔除


# ── 决策矩阵 ────────────────────────────────────────────
def test_decide_matrix():
    assert R.decide({"status": "pass", "issues": []}) == "ready"
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "high", "needs_source": False}]
                     }) == "human"
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "medium", "needs_source": True}]
                     }) == "human"               # 缺来源 → 人工
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "low", "needs_source": False}]
                     }) == "ready"
    assert R.decide({"status": "unavailable", "issues": []}) == "retry"


def test_decide_with_sapi_flag_forces_human():
    # 使用了 SAPI 机械音兜底 → 即使 AI 审核通过也强制人工裁决（评审#6）
    assert R.decide_with_flags({"status": "pass", "issues": []},
                               sapi_used=True) == "human"
    assert R.decide_with_flags({"status": "pass", "issues": []},
                               sapi_used=False) == "ready"
    # 审核通道不可用优先 retry（等恢复后连同机械音标记一起重新裁决）
    assert R.decide_with_flags({"status": "unavailable", "issues": []},
                               sapi_used=True) == "retry"


def test_run_review_combines_layers(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: {
        "verdict": "pass", "issues": []})
    doc = _doc()
    doc["sections"][1]["text"] = doc["sections"][0]["text"]
    res = R.run_review("k", doc)
    assert res["status"] == "flagged"           # 确定性 high 直接 flag
    assert res["deterministic_issues"]
