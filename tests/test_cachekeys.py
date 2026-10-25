# -*- coding: utf-8 -*-
import pytest

from src import cachekeys as C


def test_stable_hash_deterministic_and_sensitive():
    a = C.stable_hash({"x": 1, "y": "ab"})
    b = C.stable_hash({"y": "ab", "x": 1})      # 键序无关
    c = C.stable_hash({"x": 2, "y": "ab"})      # 内容变 → 指纹变
    assert a == b and a != c and len(a) == 16


def test_script_fingerprint_changes_on_query():
    d1 = {"title": "T", "sections": [{"text": "a", "video_query": "q1"}]}
    d2 = {"title": "T", "sections": [{"text": "a", "video_query": "q2"}]}
    assert C.script_fingerprint(d1) != C.script_fingerprint(d2)
    assert C.script_fingerprint(d1) == C.script_fingerprint(dict(d1))


def test_config_fingerprint_ignores_topic_pool():
    cfg = {"video": {"fps": 30}, "narrator": {"backend": "edge", "voice": "v"},
           "topics": ["A"]}
    cfg2 = dict(cfg, topics=["A", "B"])          # 选题池变化不影响进行中任务
    assert C.config_fingerprint(cfg, "v1") == C.config_fingerprint(cfg2, "v1")
    cfg3 = dict(cfg, narrator={"backend": "zhipu", "voice": "v"})
    assert C.config_fingerprint(cfg, "v1") != C.config_fingerprint(cfg3, "v1")


def test_file_hash(tmp_path):
    f = tmp_path / "a.bin"
    assert C.file_hash(f) is None
    f.write_bytes(b"hello world")
    h = C.file_hash(f)
    assert h and len(h) == 16
    f.write_bytes(b"changed")
    assert C.file_hash(f) != h


def test_narration_fingerprint_covers_inputs():
    base = dict(text="t", voice="v", rate="+0%", backend="edge", script_hash="s")
    f1 = C.narration_fingerprint(**base)
    assert f1 == C.narration_fingerprint(**base)
    for k in base:
        changed = dict(base)
        changed[k] = base[k] + "x"
        assert C.narration_fingerprint(**changed) != f1, k
