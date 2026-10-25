# -*- coding: utf-8 -*-
import time

from src.costs import Ledger, Timer


def test_ledger_records_and_aggregates(tmp_path):
    lg = Ledger(tmp_path / "costs.jsonl")
    lg.record_llm("script", {"calls": 2, "real_tokens": 0,
                             "tokens_estimated": True,
                             "chars_in": 100, "chars_out": 50}, "glm-4-flash")
    lg.record_llm("review", {"calls": 1, "real_tokens": 33,
                             "tokens_estimated": False,
                             "chars_in": 10, "chars_out": 5}, "glm-4-flash")
    lg.record("tts", "narration", backend="edge", sections=3,
              audio_seconds=12.5)
    lg.record_ffmpeg("compose", 3.2)

    s = lg.summary()
    assert s["llm"]["count"] == 2
    assert s["llm"]["total_real_tokens"] == 33
    assert s["llm"]["any_tokens_estimated"] is True    # 估算必须有标注
    assert s["tts"]["total_sections"] == 3
    assert abs(s["tts"]["total_seconds"] - 12.5) < 0.01
    assert abs(s["ffmpeg"]["total_seconds"] - 3.2) < 0.01
    assert s["ffmpeg"]["any_ok"] is True


def test_ledger_lines_roundtrip(tmp_path):
    lg = Ledger(tmp_path / "costs.jsonl")
    lg.record("materials", "materials", searches=2, downloads=1, cache_hits=1)
    lines = lg.lines()
    assert lines[0]["service"] == "materials"
    assert lines[0]["searches"] == 2
    assert "ts" in lines[0]


def test_timer_measures():
    # sleep 50ms：Windows 的 monotonic 分辨率约 15.6ms（Python<3.13），
    # sleep(0.01) 会测出 0.0 造成偶发失败
    with Timer() as t:
        time.sleep(0.05)
    assert t.seconds > 0
