# -*- coding: utf-8 -*-
"""成片技术质检（P1-6）：发布前自动体检，不达标不许进入 upload_info 产出。

检查项（对应整改方案 P1-6 表格）：
  文件 / 可解码 / 规格与时长 / 音频（静音·削波）/ 黑帧 / 字幕 / 封面

注意：音量、黑帧、静音阈值需要用真实样片校准后调整 config.yaml 的 qc 段，
不要把这里的默认值当成"经过验证的合格线"。只做"明显坏片"拦截。

只依赖 imageio_ffmpeg 自带的 ffmpeg（项目不强制安装 ffprobe），全部用
ffmpeg 自身解析。每个检查独立返回，错误级别 = error（拦截）/ warning（放行）。
"""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

# 默认阈值 —— 待真实样片校准，可在 config.yaml qc: 下覆盖
DEFAULT_THRESHOLDS = {
    "duration_tolerance_s": 2.5,   # 成片总长 vs 旁白总长 允许误差
    "max_silence_s": 8.0,          # 音频最长连续静音
    "min_mean_volume_db": -45.0,   # 平均音量低于此 → 近乎无声
    "clip_threshold_db": -0.5,     # 峰值高于此 → 削波
    "max_black_start_s": 1.5,      # 开头黑帧容忍（fadeblack 转场可能产生）
    "max_black_ratio": 0.35,       # 全片黑帧占比上限
    "min_cover_bytes": 2048,
    "decode_timeout_s": 1800,
}


def _ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args, timeout):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def probe_media(path) -> dict:
    """用 ffmpeg -i 的 stderr 解析 duration/width/height/fps/has_audio。"""
    r = _run([_ffmpeg(), "-i", str(path)], 60)
    err = r.stderr
    out = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
           "has_audio": False, "has_video": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
    if m:
        out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Stream #\d+:\d+.*?: Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    if "Video:" in err:
        out["has_video"] = True
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        out["fps"] = float(m.group(1))
    out["has_audio"] = "Audio:" in err
    return out


def _add(results, check, ok, severity="error", detail=""):
    results.append({"check": check, "ok": ok, "severity": severity,
                    "detail": detail})


def check_file(final: Path, results):
    if not final.is_file():
        _add(results, "file", False, "error", f"missing: {final}")
        return False
    size = final.stat().st_size
    if size <= 0:
        _add(results, "file", False, "error", "file is empty")
        return False
    _add(results, "file", True, "detail", f"{size/1e6:.1f} MB")
    return True


def check_decode(final: Path, results, timeout: float):
    """完整解码一遍，收集解码错误（损坏帧/码流错误）。"""
    try:
        r = _run([_ffmpeg(), "-v", "error", "-i", str(final), "-f", "null", "-"],
                 timeout)
        errs = [l for l in r.stderr.splitlines() if l.strip()][:10]
        if r.returncode != 0 or errs:
            _add(results, "decode", False, "error",
                 f"decode errors: {errs or r.returncode}")
        else:
            _add(results, "decode", True, "detail", "clean decode")
    except subprocess.TimeoutExpired:
        _add(results, "decode", False, "error", f"decode timed out ({timeout}s)")


def check_spec(probe: dict, spec: tuple, expected_duration: float,
               tol: float, results):
    w, h, fps = spec
    if probe["has_video"] and (probe["width"], probe["height"]) != (w, h):
        _add(results, "resolution", False, "error",
             f"{probe['width']}x{probe['height']} != expected {w}x{h}")
    else:
        _add(results, "resolution", True)
    if fps and probe["fps"] and abs(probe["fps"] - fps) > 0.5:
        _add(results, "fps", False, "warning",
             f"{probe['fps']} fps != expected {fps}")
    if probe["duration"] <= 0:
        _add(results, "duration", False, "error", "could not read duration")
    elif expected_duration and abs(probe["duration"] - expected_duration) > tol:
        _add(results, "duration", False, "error",
             f"video {probe['duration']:.1f}s vs narration {expected_duration:.1f}s "
             f"(tol {tol}s) — 音画可能错位")
    else:
        _add(results, "duration", True,
             detail=f"{probe['duration']:.1f}s" if probe["duration"] else "")
    if not probe["has_audio"]:
        _add(results, "audio_stream", False, "error", "no audio stream")


def check_audio(final: Path, th: dict, results):
    """volumedetect + silencedetect 一趟完成。"""
    try:
        r = _run([_ffmpeg(), "-i", str(final), "-vn",
                  "-af", f"volumedetect,silencedetect=noise=-35dB:d={th['max_silence_s']}",
                  "-f", "null", "-"], th["decode_timeout_s"])
        err = r.stderr
        mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
        peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
        if mean:
            mv = float(mean.group(1))
            if mv < th["min_mean_volume_db"]:
                _add(results, "audio_volume", False, "error",
                     f"near-silence: mean {mv} dB < {th['min_mean_volume_db']} dB")
            else:
                _add(results, "audio_volume", True, detail=f"mean {mv} dB")
        else:
            _add(results, "audio_volume", False, "warning", "no volume stats parsed")
        if peak:
            pv = float(peak.group(1))
            if pv > th["clip_threshold_db"]:
                _add(results, "audio_clipping", False, "warning",
                     f"possible clipping: max {pv} dB > {th['clip_threshold_db']} dB")
        sils = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", err)]
        if sils and max(sils) > th["max_silence_s"]:
            _add(results, "audio_silence", False, "error",
                 f"long silence {max(sils):.1f}s > {th['max_silence_s']}s")
        else:
            _add(results, "audio_silence", True,
                 detail=f"max {max(sils):.1f}s" if sils else "none detected")
    except subprocess.TimeoutExpired:
        _add(results, "audio", False, "warning", "audio scan timed out")


def check_black(final: Path, th: dict, results):
    try:
        r = _run([_ffmpeg(), "-i", str(final), "-an",
                  "-vf", "blackdetect=d=0.5:pix_th=0.10",
                  "-f", "null", "-"], th["decode_timeout_s"])
        blacks = [(float(a), float(b), float(c)) for a, b, c in re.findall(
            r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)",
            r.stderr)]
        if not blacks:
            _add(results, "black_frames", True, detail="none")
            return
        total = sum(b[2] for b in blacks)
        probe = probe_media(final)
        ratio = total / probe["duration"] if probe["duration"] else 1.0
        starts_bad = any(b[0] < 0.5 and b[1] > th["max_black_start_s"] for b in blacks)
        if ratio > th["max_black_ratio"] or starts_bad:
            _add(results, "black_frames", False, "error",
                 f"black ratio {ratio:.2f}, first black ends at "
                 f"{blacks[0][1]:.1f}s (limit {th['max_black_start_s']}s)")
        else:
            _add(results, "black_frames", True,
                 detail=f"{len(blacks)} segments, ratio {ratio:.2f}")
    except subprocess.TimeoutExpired:
        _add(results, "black_frames", False, "warning", "black scan timed out")


# ── 字幕 ────────────────────────────────────────────────
_DEBUG_RESIDUE = re.compile(r"TODO|FIXME|XXX|<think>|as an ai", re.I)
_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _ts_seconds(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def validate_srt(srt_path, max_duration: float = None) -> list:
    """字幕时间轴校验：可解析、不倒退、不超时长、无空白/调试残留。"""
    problems = []
    p = Path(srt_path)
    if not p.is_file():
        return [f"srt missing: {p}"]
    text = p.read_text(encoding="utf-8", errors="replace")
    cues = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if not cues:
        return ["srt has no cues"]
    prev_end = -1.0
    for i, block in enumerate(cues, 1):
        m = _TS.search(block)
        if not m:
            problems.append(f"cue {i}: unparseable timestamp")
            continue
        start = _ts_seconds(*m.group(1, 2, 3, 4))
        end = _ts_seconds(*m.group(5, 6, 7, 8))
        if end <= start:
            problems.append(f"cue {i}: end <= start ({start:.2f} --> {end:.2f})")
        if start < prev_end - 0.05:
            problems.append(f"cue {i}: timeline goes backwards ({start:.2f} < {prev_end:.2f})")
        prev_end = max(prev_end, end)
        if max_duration is not None and end > max_duration + 2.0:
            problems.append(f"cue {i}: ends at {end:.2f}s beyond video {max_duration:.2f}s")
        body = _TS.sub("", block).strip()
        body = "\n".join(l for l in body.splitlines() if not l.strip().isdigit())
        if not body.strip():
            problems.append(f"cue {i}: empty text")
        elif _DEBUG_RESIDUE.search(body):
            problems.append(f"cue {i}: debug residue in subtitle text")
    return problems


def check_subtitle(srt_path, video_duration: float, results):
    problems = validate_srt(srt_path, video_duration)
    if problems:
        _add(results, "subtitle", False, "error", "; ".join(problems[:6]))
    else:
        _add(results, "subtitle", True)


def check_cover(cover_path, th: dict, results):
    p = Path(cover_path)
    if not p.is_file():
        _add(results, "cover", False, "error", f"missing: {p}")
        return
    data = p.read_bytes()
    if len(data) < th["min_cover_bytes"]:
        _add(results, "cover", False, "error",
             f"too small ({len(data)} B < {th['min_cover_bytes']} B)")
    elif not data.startswith(b"\xff\xd8"):
        _add(results, "cover", False, "error", "not a JPEG file")
    else:
        _add(results, "cover", True, detail=f"{len(data)/1024:.0f} KB")


def run_qc(final, srt, cover, expected_duration: float = 0.0,
           spec: tuple = (1920, 1080, 30), thresholds: dict = None) -> tuple:
    """完整质检。返回 (ok: bool, results: list[dict])。ok=False 时上游必须拦截。"""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    final, srt, cover = Path(final), Path(srt), Path(cover)
    results = []
    if not check_file(final, results):
        return False, results
    probe = probe_media(final)
    check_decode(final, results, th["decode_timeout_s"])
    check_spec(probe, spec, expected_duration, th["duration_tolerance_s"], results)
    check_audio(final, th, results)
    check_black(final, th, results)
    check_subtitle(srt, probe["duration"], results)
    check_cover(cover, th, results)
    ok = not any(r["ok"] is False and r["severity"] == "error" for r in results)
    return ok, results
