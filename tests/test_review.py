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


def test_ai_malformed_issues_fail_closed(monkeypatch):
    """含非法元素的问题列表 → 整包拒绝（fail-closed），不再是"过滤坏数据"。（评审 P1）"""
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: {
        "verdict": "flagged",
        "issues": ["invalid item", 42,
                   {"section": 0, "severity": "low", "issue": "ok"}]})
    res = R.ai_review("k", _doc())
    assert res["status"] == "unavailable"       # 有非法元素 → 整包不可用
    assert res["raw_verdict"] is None


def _resp(verdict="pass", issues=None):
    return {"verdict": verdict,
            "issues": issues if issues is not None else []}


def test_ai_rejects_unknown_or_missing_verdict(monkeypatch):
    """verdict 缺失/未知/类型错误 → 一律 unavailable（fail-closed）。（评审 P1）"""
    for bad in ["approved", "ok", "", None, 1, True, ["pass"], {"v": 1}]:
        monkeypatch.setattr(R, "_llm_json",
                            lambda *a, _v=bad, **k: _resp(_v))
        res = R.ai_review("k", _doc())
        assert res["status"] == "unavailable", bad
        assert res["raw_verdict"] is None


def test_ai_accepts_case_insensitive_verdict(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: _resp("  PASS  "))
    assert R.ai_review("k", _doc())["status"] == "pass"


def test_ai_rejects_non_object_response(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: ["not", "a", "dict"])
    assert R.ai_review("k", _doc())["status"] == "unavailable"


def test_ai_rejects_non_list_issues(monkeypatch):
    for bad_issues in ["no list", {"a": 1}, 42, None]:
        # 直接构造原始响应，避免 _resp 把 None 归一化成 []
        monkeypatch.setattr(R, "_llm_json",
                            lambda *a, _i=bad_issues, **k:
                            {"verdict": "pass", "issues": _i})
        res = R.ai_review("k", _doc())
        assert res["status"] == "unavailable", bad_issues


@pytest.mark.parametrize("bad", [
    "garbage",                                                   # 非字典
    42,                                                          # 非字典
    {"severity": "high", "issue": "x"},                          # 缺 section
    {"section": 0, "issue": "x"},                                # 缺 severity
    {"section": 0, "severity": "critical", "issue": "x"},        # 非法 severity
    {"section": "0", "severity": "high", "issue": "x"},          # section 类型错
    {"section": True, "severity": "high", "issue": "x"},         # bool 冒充 int
    {"section": -2, "severity": "high", "issue": "x"},           # 非法段号
    {"section": 0, "severity": "high"},                          # 缺 issue
    {"section": 0, "severity": "high", "issue": "   "},          # 空描述
    {"section": 0, "severity": "high", "issue": "x",
     "needs_source": "yes"},                                     # needs_source 类型错
    {"section": 0, "severity": "high", "issue": "x",
     "suggestion": 5},                                           # suggestion 类型错
])
def test_ai_fail_closed_on_invalid_issue(monkeypatch, bad):
    monkeypatch.setattr(R, "_llm_json",
                        lambda *a, _i=bad, **k: _resp("pass", [_i]))
    res = R.ai_review("k", _doc())
    assert res["status"] == "unavailable", bad
    assert "rejected" in res["issues"][0]["issue"]


def test_ai_accepts_valid_issue_and_normalizes(monkeypatch):
    good = {"section": 0, "severity": "HIGH", "issue": "wrong year",
            "suggestion": "", "needs_source": True}
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: _resp("flagged", [good]))
    res = R.ai_review("k", _doc())
    assert res["status"] == "flagged"
    assert res["issues"][0]["severity"] == "high"
    assert res["issues"][0]["needs_source"] is True


def test_pass_with_issues_is_contradictory_fail_closed(monkeypatch):
    good = {"section": 0, "severity": "low", "issue": "nit", "needs_source": False}
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: _resp("pass", [good]))
    assert R.ai_review("k", _doc())["status"] == "unavailable"


def test_flagged_with_empty_issues_never_auto_ready(monkeypatch):
    """评审 P1 核心场景：flagged + 空问题列表 → 人工裁决，绝不自动 ready。"""
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: _resp("flagged", []))
    res = R.ai_review("k", _doc())
    assert res["status"] == "flagged"
    assert R.decide(res) == "human"


# ── 决策矩阵 ────────────────────────────────────────────
def test_decide_matrix():
    assert R.decide({"status": "pass", "issues": []}) == "ready"
    # 任何 flagged 都不允许自动放行（评审 P1）
    assert R.decide({"status": "flagged", "issues": []}) == "human"
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "low", "needs_source": False}]
                     }) == "human"
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "medium", "needs_source": True}]
                     }) == "human"
    assert R.decide({"status": "flagged",
                     "issues": [{"severity": "high", "needs_source": False}]
                     }) == "human"
    # 防御：pass 却带问题 → 交人工
    assert R.decide({"status": "pass",
                     "issues": [{"severity": "low", "needs_source": False}]
                     }) == "human"
    # fail-closed 默认：unavailable 与未知状态一律 retry
    assert R.decide({"status": "unavailable", "issues": []}) == "retry"
    assert R.decide({"status": "weird-state"}) == "retry"
    assert R.decide({}) == "retry"


def test_decide_with_flags_matrix():
    # SAPI 机械音 / 通用兜底素材，任一任务级标记都强制人工裁决（评审#3/#6）
    assert R.decide_with_flags({"status": "pass", "issues": []},
                               sapi_used=True) == "human"
    assert R.decide_with_flags({"status": "pass", "issues": []},
                               fallback_used=True) == "human"
    assert R.decide_with_flags({"status": "pass", "issues": []},
                               sapi_used=True,
                               fallback_used=True) == "human"
    assert R.decide_with_flags({"status": "pass", "issues": []}) == "ready"
    # 审核通道不可用优先 retry（等恢复后连同标记一起重新裁决）
    assert R.decide_with_flags({"status": "unavailable", "issues": []},
                               sapi_used=True,
                               fallback_used=True) == "retry"


def test_run_review_combines_layers(monkeypatch):
    monkeypatch.setattr(R, "_llm_json", lambda *a, **k: {
        "verdict": "pass", "issues": []})
    doc = _doc()
    doc["sections"][1]["text"] = doc["sections"][0]["text"]
    res = R.run_review("k", doc)
    assert res["status"] == "flagged"           # 确定性 high 直接 flag
    assert res["deterministic_issues"]
