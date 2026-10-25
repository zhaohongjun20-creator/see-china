# -*- coding: utf-8 -*-
"""评审#1：多段数真实 FFmpeg 合成验证时长公式（2/5/7/10 段）。

证明三件事：
1. expected_total_duration() 的理论公式与真实 xfade 合成结果一致（不只 2 段）；
2. 正常成片（任意段数）不会被 QC 时长检查误杀；
3. 异常时长仍然会被 QC 发现。
"""
import subprocess

import pytest
import imageio_ffmpeg

from src import qc
from src.slideshow import compose_final, expected_total_duration

FF = lambda: imageio_ffmpeg.get_ffmpeg_exe()
FADE, TAIL, END_HOLD, D = 0.6, 0.6, 1.0, 0.5   # 与 section_need 的语义一致


def _clip(path, dur):
    subprocess.run([FF(), "-y", "-f", "lavfi",
                    "-i", f"testsrc=duration={dur:.3f}:size=320x240:rate=30",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=180)


def _voice(path):
    subprocess.run([FF(), "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={D}",
                    "-f", "wav", "-c:a", "pcm_s16le", str(path)],
                   check=True, capture_output=True, timeout=60)


def _bgm(path):
    subprocess.run([FF(), "-y", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=8",
                    "-f", "wav", "-c:a", "pcm_s16le", str(path)],
                   check=True, capture_output=True, timeout=60)


def _compose_episode(tmp_path, n):
    bgm = tmp_path / "bgm.wav"
    _bgm(bgm)
    clips, voices, texts = [], [], []
    for i in range(n):
        c = tmp_path / f"clip_{i:02d}.mp4"
        v = tmp_path / f"voice_{i:02d}.wav"
        _clip(c, D + FADE + (TAIL if i < n - 1 else END_HOLD))  # = section_need
        _voice(v)
        clips.append(str(c))
        voices.append(str(v))
        texts.append({"text": f"section number {i} words " * 6})
    out = tmp_path / "final.mp4"
    srt = tmp_path / "final.srt"
    compose_final(clips, voices, texts, str(out), str(bgm), str(srt),
                  bgm_volume=0.2, fade=FADE)
    return out, srt


def test_formula_values():
    # 与手工推导一致：ΣD + fade + tail*(n-1) + end_hold（fade 只计一次）
    assert expected_total_duration([0.5, 0.5], 0.6, 0.6, 1.0) == pytest.approx(3.2)
    assert expected_total_duration([0.5], 0.6, 0.6, 1.0) == pytest.approx(2.1)
    assert expected_total_duration([0.5] * 10, 0.6, 0.6, 1.0) == pytest.approx(
        5.0 + 0.6 + 0.6 * 9 + 1.0)
    assert expected_total_duration([], 0.6, 0.6, 1.0) == 0.0


@pytest.mark.parametrize("n", [2, 5, 7, 10])
def test_expected_duration_matches_real_composite(tmp_path, n):
    out, srt = _compose_episode(tmp_path, n)
    expected = expected_total_duration([D] * n, FADE, TAIL, END_HOLD)
    measured = qc.probe_media(out)["duration"]
    assert abs(measured - expected) < 0.6, \
        f"n={n}: measured {measured:.2f}s vs expected {expected:.2f}s"

    cover = tmp_path / "cover.jpg"
    subprocess.run([FF(), "-y", "-ss", "1", "-i", str(out), "-frames:v", "1",
                    str(cover)], check=True, capture_output=True, timeout=60)
    ok, results = qc.run_qc(out, srt, cover, expected_duration=expected,
                            spec=(320, 240, 30))
    errs = [r for r in results if not r["ok"] and r["severity"] == "error"]
    assert ok, f"n={n} 正常成片被 QC 误杀: {errs}"


def test_qc_still_catches_wrong_duration(tmp_path):
    out, srt = _compose_episode(tmp_path, 5)
    expected = expected_total_duration([D] * 5, FADE, TAIL, END_HOLD)
    ok, results = qc.run_qc(out, srt, tmp_path / "no_cover.jpg",
                            expected_duration=expected + 5.0,
                            spec=(320, 240, 30))
    assert not ok
    assert any(r["check"] == "duration" and not r["ok"] for r in results)
