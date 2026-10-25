# -*- coding: utf-8 -*-
"""narrator 降级链 / CosyVoice 批量分发 / 探针分类 / doctor 报告测试。

全部 mock，不触网、不加载模型、不调用真实 cosyvoice 环境。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from src import narrator as N


# ── 逐段降级链 ──────────────────────────────────────────
def test_chain_edge_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "_synth_edge", lambda *a, **k: None)
    path, backend, voice = N.synth_section("t", str(tmp_path / "a.mp3"),
                                           "en-V", "+0%")
    assert backend == "edge" and voice == "en-V"


def test_chain_piper_takes_over_when_edge_blocked(monkeypatch, tmp_path):
    """edge 封堵（403）→ 音色熔断池全灭 → Piper 本地神经接管。"""
    def fail(*a, **k):
        raise RuntimeError("403 blocked")
    monkeypatch.setattr(N, "_synth_edge", fail)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    monkeypatch.setattr(N, "_synth_piper", lambda text, out: str(wav))
    path, backend, voice = N.synth_section("t", str(tmp_path / "a.mp3"),
                                           "en-V", "+0%")
    assert backend == "piper"
    assert "ryan" in voice                        # 英文模型 en_US-ryan-medium


def test_quality_gate_blocks_sapi_by_default(monkeypatch, tmp_path):
    """edge 与 Piper 全灭且未显式放行 → 停线（绝不静默用机械音）。"""
    def fail(*a, **k):
        raise RuntimeError("403")
    monkeypatch.setattr(N, "_synth_edge", fail)
    monkeypatch.setattr(N, "_synth_piper", fail)
    with pytest.raises(RuntimeError, match="质量闸门"):
        N.synth_section("t", str(tmp_path / "a.mp3"), "v", "+0%",
                        allow_sapi=False)


def test_sapi_only_with_explicit_allow(monkeypatch, tmp_path):
    def fail(*a, **k):
        raise RuntimeError("403")
    monkeypatch.setattr(N, "_synth_edge", fail)
    monkeypatch.setattr(N, "_synth_piper", fail)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    monkeypatch.setattr(N, "_synth_sapi", lambda text, out: str(wav))
    path, backend, voice = N.synth_section("t", str(tmp_path / "a.mp3"), "v",
                                           "+0%", allow_sapi=True)
    assert backend == "sapi"


def test_zhipu_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "_synth_zhipu", lambda *a, **k: None)
    path, backend, voice = N.synth_section(
        "t", str(tmp_path / "a.mp3"), "v", "+0%", backend="zhipu",
        zhipu={"api_key": "k", "voice": "tongtong"})
    assert backend == "zhipu" and voice == "tongtong"


def test_zhipu_failure_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "_synth_zhipu",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("402")))
    monkeypatch.setattr(N, "_synth_edge", lambda *a, **k: None)
    path, backend, voice = N.synth_section(
        "t", str(tmp_path / "a.mp3"), "v", "+0%", backend="zhipu",
        zhipu={"api_key": "k"})
    assert backend == "edge"                      # zhipu 失败降级 edge


# ── CosyVoice 批量分发 ──────────────────────────────────
def test_cosy_batch_manifest_and_indices(monkeypatch, tmp_path):
    py, cli = tmp_path / "python.exe", tmp_path / "cli.py"
    py.write_text("")
    cli.write_text("")
    monkeypatch.setattr(N, "_COSY_PY", py)
    monkeypatch.setattr(N, "_COSY_CLI", cli)
    manifest = tmp_path / "_jobs_seechina.json"
    monkeypatch.setattr(N, "_COSY_MANIFEST", manifest)

    secs = [{"text": "hello world"}, {"text": "second section"}]
    (tmp_path / "out").mkdir()                    # 批量输出目录（真实流程由任务目录保证）

    def fake_run(cmd, **kw):
        jobs = json.loads(manifest.read_text(encoding="utf-8"))["jobs"]
        assert len(jobs) == 2
        assert jobs[0]["text"] == "hello world"
        assert jobs[0]["mode"] == "instruct2"
        assert jobs[1]["instruct"] == "speak warm"
        assert str(tmp_path) in jobs[0]["out"]    # 输出按原段号落盘
        for j in jobs:
            Path(j["out"]).write_bytes(b"x")
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()
    monkeypatch.setattr(N.subprocess, "run", fake_run)

    outs = N.synth_cosy_batch(
        secs, str(tmp_path / "out"),
        {"mode": "instruct2", "prompt_wav": "p.wav", "instruct": "speak warm"},
        indices=[3, 7])
    assert outs[0].endswith("sec_03.wav")
    assert outs[1].endswith("sec_07.wav")


def test_cosy_batch_incomplete_raises(monkeypatch, tmp_path):
    py, cli = tmp_path / "python.exe", tmp_path / "cli.py"
    py.write_text("")
    cli.write_text("")
    monkeypatch.setattr(N, "_COSY_PY", py)
    monkeypatch.setattr(N, "_COSY_CLI", cli)
    monkeypatch.setattr(N, "_COSY_MANIFEST", tmp_path / "m.json")
    monkeypatch.setattr(N.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="cosy batch incomplete"):
        N.synth_cosy_batch([{"text": "t"}], str(tmp_path), {})


def test_cosy_env_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "_COSY_PY", tmp_path / "missing" / "python.exe")
    with pytest.raises(RuntimeError, match="cosyvoice env missing"):
        N.synth_cosy_batch([{"text": "t"}], str(tmp_path), {})


def test_cosy_env_ready(tmp_path, monkeypatch):
    py, cli = tmp_path / "python.exe", tmp_path / "cli.py"
    py.write_text("")
    cli.write_text("")
    monkeypatch.setattr(N, "_COSY_PY", py)
    monkeypatch.setattr(N, "_COSY_CLI", cli)
    assert N.cosy_env_ready() is True
    monkeypatch.setattr(N, "_COSY_PY", tmp_path / "nope.exe")
    assert N.cosy_env_ready() is False


# ── 探针分类 ────────────────────────────────────────────
def test_probe_edge_ok(monkeypatch):
    class _Ok:
        def __init__(self, *a):
            pass

        async def save(self, out):
            await asyncio.sleep(0)
    monkeypatch.setattr(N, "edge_tts",
                        types.SimpleNamespace(Communicate=_Ok))
    assert N.probe_edge() == "ok"


def test_probe_edge_blocked403(monkeypatch):
    class _Blocked:
        def __init__(self, *a):
            pass

        async def save(self, out):
            raise RuntimeError("403 Invalid response status")
    monkeypatch.setattr(N, "edge_tts",
                        types.SimpleNamespace(Communicate=_Blocked))
    assert N.probe_edge() == "blocked403"


def test_probe_edge_network(monkeypatch):
    class _NetErr:
        def __init__(self, *a):
            pass

        async def save(self, out):
            raise ConnectionError("Cannot connect to host speech.platform.bing.com")
    monkeypatch.setattr(N, "edge_tts",
                        types.SimpleNamespace(Communicate=_NetErr))
    assert N.probe_edge() == "network"


def test_probe_piper_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "piper", None)
    assert N.probe_piper() == "missing:package"


def test_probe_piper_missing_model(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "PIPER_MODEL", str(tmp_path / "nope.onnx"))
    assert N.probe_piper() == "missing:model"


# ── doctor 报告 ─────────────────────────────────────────
def test_tts_report_rows(monkeypatch):
    import autopost as A
    monkeypatch.setattr(A.narrator, "probe_edge", lambda *a, **k: "blocked403")
    monkeypatch.setattr(A.narrator, "probe_piper", lambda: "ok")
    monkeypatch.setattr(A.narrator, "cosy_env_ready", lambda: True)
    monkeypatch.setattr(A.os, "environ", {**A.os.environ, "ZHIPU_API_KEY": "k"})
    rows = A.tts_report({"narrator": {"cosy": {"enable": True}}})
    assert [r[0] for r in rows] == ["edge-tts", "piper", "cosyvoice2",
                                    "zhipu cogtts"]
    assert rows[0][1] == "blocked403"
    assert rows[1][1] == "ok"
    assert rows[2][1] == "ready"


def test_tts_report_cosy_disabled(monkeypatch):
    import autopost as A
    monkeypatch.setattr(A.narrator, "probe_edge", lambda *a, **k: "ok")
    monkeypatch.setattr(A.narrator, "probe_piper", lambda: "ok")
    rows = A.tts_report({"narrator": {}})
    assert "cosyvoice2" not in [r[0] for r in rows]
