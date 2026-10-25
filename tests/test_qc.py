# -*- coding: utf-8 -*-
"""qc 模块测试：字幕/封面用纯逻辑样例，音视频检查用 ffmpeg 现做 1-3 秒小样片。"""
import subprocess

import pytest
import imageio_ffmpeg

from src import qc


def ff():
    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope="module")
def testsrc_video(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    v = d / "testsrc.mp4"
    subprocess.run([ff(), "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=320x240:rate=30",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(v)],
                   check=True, capture_output=True, timeout=180)
    return v


# ── 字幕 ────────────────────────────────────────────────
def test_validate_srt_ok(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello there\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nsecond line\n", encoding="utf-8")
    assert qc.validate_srt(srt, 2.5) == []


def test_validate_srt_detects_problems(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:00,500\nbackwards time\n\n"   # 倒退
        "2\n00:00:00,000 --> 00:00:01,000\n\n\n"                  # 空文本
        "3\n00:00:02,000 --> 00:00:30,000\nTODO fix me\n",        # 越界+调试残留
        encoding="utf-8")
    ps = qc.validate_srt(srt, 3.0)
    assert any("end <= start" in p for p in ps)
    assert any("empty text" in p for p in ps)
    assert any("debug residue" in p for p in ps)
    assert any("beyond video" in p for p in ps)


def test_validate_srt_missing_file(tmp_path):
    assert qc.validate_srt(tmp_path / "nope.srt") == ["srt missing: " +
                                                      str(tmp_path / "nope.srt")]


# ── 封面 ────────────────────────────────────────────────
def test_cover_checks(tmp_path, testsrc_video):
    cover = tmp_path / "c.jpg"
    subprocess.run([ff(), "-y", "-ss", "0.5", "-i", str(testsrc_video),
                    "-frames:v", "1", str(cover)],
                   check=True, capture_output=True, timeout=60)
    res = []
    qc.check_cover(cover, qc.DEFAULT_THRESHOLDS, res)
    assert res[-1]["ok"]

    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not a jpeg")
    res2 = []
    qc.check_cover(bad, qc.DEFAULT_THRESHOLDS, res2)
    assert not res2[-1]["ok"]

    res3 = []
    qc.check_cover(tmp_path / "missing.jpg", qc.DEFAULT_THRESHOLDS, res3)
    assert not res3[-1]["ok"]


# ── 整体质检（真实小样片） ───────────────────────────────
@pytest.fixture
def good_episode(tmp_path):
    """3 秒 testsrc + 正弦配音轨 + 匹配 srt + 封面：应通过质检。"""
    v = tmp_path / "final.mp4"
    subprocess.run([ff(), "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(v)],
                   check=True, capture_output=True, timeout=300)
    srt = tmp_path / "final.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello there\n",
                   encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    subprocess.run([ff(), "-y", "-ss", "1", "-i", str(v), "-frames:v", "1",
                    str(cover)], check=True, capture_output=True, timeout=60)
    return v, srt, cover


def test_run_qc_passes_good_episode(good_episode):
    v, srt, cover = good_episode
    ok, results = qc.run_qc(v, srt, cover, expected_duration=3.0,
                            spec=(320, 240, 30))
    errs = [r for r in results if not r["ok"] and r["severity"] == "error"]
    assert ok, errs


def test_run_qc_flags_duration_mismatch(good_episode):
    v, srt, cover = good_episode
    ok, results = qc.run_qc(v, srt, cover, expected_duration=30.0,
                            spec=(320, 240, 30))
    assert not ok
    assert any(r["check"] == "duration" and not r["ok"] for r in results)


def test_run_qc_flags_wrong_resolution(good_episode):
    v, srt, cover = good_episode
    ok, results = qc.run_qc(v, srt, cover, expected_duration=3.0,
                            spec=(1280, 720, 30))
    assert not ok
    assert any(r["check"] == "resolution" and not r["ok"] for r in results)


def test_run_qc_missing_file(tmp_path):
    ok, results = qc.run_qc(tmp_path / "nope.mp4", tmp_path / "a.srt",
                            tmp_path / "c.jpg")
    assert not ok
    assert results[0]["check"] == "file"
