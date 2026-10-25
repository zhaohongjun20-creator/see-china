# -*- coding: utf-8 -*-
import json

import pytest
import requests

from src import script as S


def _full_doc(sections=3, query="q {}", text=None):
    return {"title": "A Test Episode",
            "description": "d",
            "tags": ["t1", "t2"],
            "sections": [{"text": text if text is not None else "word " * 60,
                          "video_query": query.format(i)}
                         for i in range(sections)]}


# ── 规范化（P0-1 核心：字段别名统一为 video_query） ────────
def test_normalize_maps_image_query_alias():
    doc = S.normalize_outline(
        {"title": "T", "sections": [{"text": "hello world", "image_query": "great wall"}]})
    assert doc["sections"][0]["video_query"] == "great wall"
    assert "image_query" not in doc["sections"][0]


def test_normalize_handles_list_and_tags_string():
    doc = S.normalize_outline({"sections": [{"query": ["a", "b", 3]}],
                               "tags": "x, y;z"})
    assert doc["sections"][0]["video_query"] == "a b 3"
    assert doc["tags"] == ["x", "y", "z"]


def test_normalize_truncates_overlong_query():
    doc = S.normalize_outline({"sections": [{"video_query": "a b c d e f g h"}]})
    assert len(doc["sections"][0]["video_query"].split()) <= 6


def test_normalize_non_object_raises():
    with pytest.raises(S.ScriptError):
        S.normalize_outline(["not", "a", "dict"])


# ── 校验 ────────────────────────────────────────────────
def test_validate_missing_query_is_hard():
    doc = _full_doc()
    doc["sections"][1]["video_query"] = ""
    ps = S.validate(doc, 3, 5, 55, 95)
    assert any(p["level"] == "hard" and p["section"] == 1 for p in ps)


def test_validate_outline_phase_ignores_empty_text():
    doc = _full_doc(text="")
    ps = S.validate(doc, 3, 5, 55, 95, require_text=False)
    assert not any(p["level"] == "hard" for p in ps)


def test_validate_final_requires_text():
    doc = _full_doc(text="")
    ps = S.validate(doc, 3, 5, 55, 95, require_text=True)
    assert any(p["level"] == "hard" for p in ps)


# ── generate 全流程（monkeypatch 掉 LLM 调用） ────────────
def test_generate_two_step_with_expansion(monkeypatch):
    calls = []

    def fake_call(api_key, model, prompt, temperature, usage):
        calls.append(prompt)
        usage.add(None, prompt, "x")            # 与真实 _call 一样记账
        if "STRICT JSON" in prompt:
            d = _full_doc(text="")                  # 大纲：文本留空待扩写
            return json.dumps(d)
        return "word " * 60                          # 扩写
    monkeypatch.setattr(S, "_call", fake_call)
    doc = S.generate("k", "topic", n_min=3, n_max=3)
    assert len(doc["sections"]) == 3
    assert all(s["text"] and s["video_query"] for s in doc["sections"])
    assert len(calls) == 1 + 3                       # 1 大纲 + 3 扩写
    assert doc["_usage"]["calls"] == 4
    assert doc["_usage"]["tokens_estimated"] is True


def test_generate_fixes_broken_outline(monkeypatch):
    calls = []

    def fake_call(api_key, model, prompt, temperature, usage):
        calls.append(prompt)
        usage.add(None, prompt, "x")
        if "problems" in prompt:                     # 修复轮
            return json.dumps(_full_doc(text=""))
        d = _full_doc(text="")
        d["sections"][0]["video_query"] = ""         # 大纲缺取材词
        return json.dumps(d)
    monkeypatch.setattr(S, "_call", fake_call)
    doc = S.generate("k", "topic", n_min=3, n_max=3)
    assert all(s["video_query"] for s in doc["sections"])
    assert any("problems" in c for c in calls)       # 确实触发过修复


def test_generate_gives_up_after_fix_attempts(monkeypatch):
    def fake_call(api_key, model, prompt, temperature, usage):
        usage.add(None, prompt, "x")
        if "problems" in prompt:
            d = _full_doc(text="")                   # 修复后仍缺 query
            d["sections"][0]["video_query"] = ""
            return json.dumps(d)
        d = _full_doc(text="")
        d["sections"][0]["video_query"] = ""
        return json.dumps(d)
    monkeypatch.setattr(S, "_call", fake_call)
    with pytest.raises(S.ScriptError) as ei:
        S.generate("k", "topic", n_min=3, n_max=3, max_fix_attempts=1)
    assert ei.value.problems


def test_generate_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(S, "_call", lambda *a: "this is not json at all")
    with pytest.raises(S.ScriptError):
        S.generate("k", "topic", n_min=3, n_max=3)


def test_call_error_classification(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code = code

    def net(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(S.requests, "post", net)
    usage = S._Usage()
    with pytest.raises(S.ScriptError, match="network"):
        S._call("k", "m", "p", 0.5, usage)

    monkeypatch.setattr(S.requests, "post", lambda *a, **k: Resp(401))
    with pytest.raises(S.ScriptError, match="auth"):
        S._call("k", "m", "p", 0.5, usage)

    monkeypatch.setattr(S.requests, "post", lambda *a, **k: Resp(429))
    with pytest.raises(S.ScriptError, match="rate"):
        S._call("k", "m", "p", 0.5, usage)


def test_usage_estimation_flag():
    u1 = S._Usage()
    u1.add(None, "prompt", "out")                      # 服务端没给 usage
    d = u1.as_dict()
    assert d["tokens_estimated"] is True and d["real_tokens"] == 0

    u2 = S._Usage()
    u2.add({"total_tokens": 42}, "p", "o")             # 服务端给了 usage
    d2 = u2.as_dict()
    assert d2["real_tokens"] == 42 and d2["tokens_estimated"] is False
