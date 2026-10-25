# -*- coding: utf-8 -*-
"""成片合成（P1-6 配套重写：修复无音轨片段 concat 必败 + 音画对齐漂移）。

旧实现的两个问题：
1. section_clip.build 输出 -an（无音轨）片段，compose_final 阶段 B 却按
   [i:a] 对片段做音频 concat → 运行期必报错。
2. 字幕/旁白按 sum(duration+tail) 推进，而 xfade 视频轨每个接缝缩短 fade 秒，
   每段漂移 fade 秒，段数越多旁白越超前于画面。

新时间线（统一基准 T_i = 段 i 画面完整可见的时刻）：
    转场溶解占 [T_i - fade, T_i)；旁白 i 用 adelay 精确从 T_i 开始；
    旁白 i 结束后画面停留 tail 秒才开始溶解 → 旁白永远踩在自己的画面上。
    片段仍为纯视频轨；音频轨由旁白文件 + 延迟合成，BGM 在收尾混音时压限。

三步结构保留（一步大 filter 图会因 apad 无限流+多级 xfade 死锁）：
    A. 仅视频：多级 xfade（样式轮换）
    B. 仅音频：旁白 aformat + adelay(T_i) + amix → 纯人声轨
    C. 收尾：字幕烧录 + 人声/BGM 混音（sidechain ducking）
"""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

W, H, FPS = 1920, 1080, 30

# 纪录片字幕样式：白字黑边+轻阴影
SUBTITLE_STYLE = ("FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,"
                  "OutlineColour=&H96000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=30,"
                  "Alignment=2")

# 段落转场轮换池（xfade 纪录片向：相邻切换不重样）
DEFAULT_TRANSITIONS = ["fade", "dissolve", "wipeleft", "circleopen", "fadeblack"]


def _ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration(path: str) -> float:
    r = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True,
                       text=True, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        raise RuntimeError(f"cannot read duration of {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_srt(narrations: list, path: str, starts: list = None) -> str:
    """每段旁白一条字幕。starts 给定时按段画面起点对齐（新时间线）；
    否则按旁白顺序累计（旧行为，向后兼容）。"""
    lines = []
    if starts is None:
        t0, starts = 0.0, []
        for n in narrations:
            starts.append(t0)
            t0 += n["duration"] + n.get("tail", 0.0)
    for i, n in enumerate(narrations):
        lines += [str(i + 1),
                  f"{_fmt_ts(starts[i])} --> {_fmt_ts(starts[i] + n['duration'])}",
                  n["text"].replace("\n", " "), ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return path


# ── A. 仅视频：多级 xfade ───────────────────────────────
def _xfade_video(section_files: list, durs: list, fade: float,
                 transitions: list, v_only: str):
    """返回 (starts, video_len)。starts[i] = 段 i 完整可见时刻。"""
    styles = transitions or DEFAULT_TRANSITIONS
    n = len(section_files)
    fc_parts, acc, cur = [f"[0:v]settb=AVTB[b0]"], durs[0], "[b0]"
    starts = [0.0]
    for i in range(1, n):
        offset = max(0.0, acc - fade)
        starts.append(offset + fade)
        style = styles[(i - 1) % len(styles)]
        fc_parts.append(f"[{i}:v]settb=AVTB,format=yuv420p[s{i}]")
        fc_parts.append(f"{cur}[s{i}]xfade=transition={style}:duration={fade}:"
                        f"offset={offset:.3f}[b{i}]")
        cur = f"[b{i}]"
        acc = offset + durs[i]
    cmd = [_ffmpeg(), "-y"]
    for f in section_files:
        cmd += ["-i", str(Path(f).resolve())]
    cmd += ["-filter_complex", ";".join(fc_parts), "-map", cur, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", v_only]
    subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
    return starts, acc


# ── B. 仅音频：旁白延迟对齐 ─────────────────────────────
def _narration_track(voice_files: list, starts: list, a_only: str):
    parts = []
    for i, (vf, t) in enumerate(zip(voice_files, starts)):
        ms = int(round(t * 1000))
        parts.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                     f"adelay={ms}:all=1[n{i}]")
    mix = "".join(f"[n{i}]" for i in range(len(voice_files))) + \
        f"amix=inputs={len(voice_files)}:duration=longest:normalize=0[aout]"
    cmd = [_ffmpeg(), "-y"]
    for f in voice_files:
        cmd += ["-i", str(Path(f).resolve())]
    cmd += ["-filter_complex", ";".join(parts) + f";{mix}", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k", a_only]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def _filter_path(p) -> str:
    """subtitles 滤镜的文件名：Windows 盘符冒号必须转义，整体加引号。"""
    s = Path(p).as_posix()
    if len(s) > 1 and s[1] == ":":
        s = s[0] + "\\:" + s[2:]        # "C:/x" → "C\:/x"（s[1] 是冒号）
    return f"'{s}'"


def expected_total_duration(voice_durations: list, fade: float, tail: float,
                            end_hold: float) -> float:
    """理论成片总长 —— QC 与合成的统一时间定义（评审#1 修复）。

    推导（与 section_need / _xfade_video 完全一致）：
      段 i 画面长 L_i = D_i + fade + tail_i（tail_{n-1} = end_hold）；
      xfade 每个接缝吞掉一个 fade → 总长 = ΣL_i - fade*(n-1)
                                      = ΣD + fade + tail*(n-1) + end_hold。
      注意 fade 只出现一次：首段画面头部的 fade 恰好被接缝抵消，
      段间旁白间隔为首段 tail+fade、其余 tail。QC 若按 (fade+tail)*(n-1)
      累加会多算 (n-2)*fade，7 段以上即可误杀正常成片。
    """
    n = len(voice_durations)
    if n == 0:
        return 0.0
    return float(sum(voice_durations)) + fade + tail * (n - 1) + end_hold


def compose_final(section_files: list, voice_files: list, narrations: list,
                  out_path: str, bgm_path: str, srt_path: str,
                  bgm_volume: float = 0.22, fade: float = 0.6,
                  tail: float = None, transitions: list = None,
                  subtitle_style: str = None) -> dict:
    """合成成片 + 字幕。返回 {starts, durations, duration, srt}。

    narrations: [{text, ...}]（text 供字幕）；旁白时长从 voice 文件实测。
    tail: 旁白结束后溶解前的停留秒数（默认 = fade，纪录片节奏）。
    """
    n = len(section_files)
    if not (n == len(voice_files) == len(narrations)) or n == 0:
        raise ValueError(f"section/voice/narration count mismatch: "
                         f"{n}/{len(voice_files)}/{len(narrations)}")
    tail = fade if tail is None else tail
    work = Path(out_path).with_suffix("")
    v_only, a_only = f"{work}_v.mp4", f"{work}_a.m4a"

    durs = [_probe_duration(f) for f in section_files]
    voice_durs = [_probe_duration(f) for f in voice_files]

    # A. 视频 xfade（同时得到每段画面起点 T_i）
    starts, video_len = _xfade_video(section_files, durs, fade, transitions, v_only)

    # B. 旁白轨：adelay 到各自 T_i
    _narration_track(voice_files, starts, a_only)

    # 字幕：旁白 i 压在 [T_i, T_i+D_i)
    cues = [{"duration": d, "text": narrations[i]["text"]}
            for i, d in enumerate(voice_durs)]
    make_srt(cues, srt_path, starts=starts)

    # C. 收尾：字幕 + ducking 混音
    style = subtitle_style or SUBTITLE_STYLE
    fc_c = (f"[1:a]asplit=2[voice][key];"
            f"[voice]apad=whole_dur={video_len:.3f}[voicep];"
            f"[2:a]volume={bgm_volume}[bg];"
            f"[bg][key]sidechaincompress=threshold=0.02:ratio=6:attack=150:release=900[bgd];"
            f"[voicep][bgd]amix=inputs=2:duration=first:normalize=0[aout];"
            f"[0:v]subtitles={_filter_path(srt_path)}"
            f":force_style='{style}'[vout]")
    cmd_c = [_ffmpeg(), "-y", "-i", v_only, "-i", a_only,
             "-stream_loop", "-1", "-i", str(bgm_path),
             "-filter_complex", fc_c,
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{video_len:.3f}", out_path]
    try:
        subprocess.run(cmd_c, check=True, capture_output=True, timeout=3600)
    finally:
        Path(v_only).unlink(missing_ok=True)
        Path(a_only).unlink(missing_ok=True)
    return {"starts": starts, "durations": durs,
            "voice_durations": voice_durs, "duration": video_len,
            "srt": str(srt_path)}
