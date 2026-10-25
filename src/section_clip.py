# -*- coding: utf-8 -*-
"""实拍片段规整：Pexels 素材 → 统一 1920x1080/30fps、裁到旁白时长（短素材循环补齐）。"""
import random, subprocess
from pathlib import Path
import imageio_ffmpeg

W, H, FPS = 1920, 1080, 30


def build(raw: str, need: float, out_mp4: str) -> dict:
    """裁一段 need 秒的画面：随机起点（素材够长时）；不够长则从头循环。

    返回 {"file", "start", "loop"} —— 使用的片段区间写入素材来源记录
    （评审要求 #5：追溯每段旁白实际用了素材的哪一段）。
    """
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    probe = subprocess.run([ff, "-i", raw], capture_output=True, text=True, timeout=60)
    dur = 0.0
    for line in probe.stderr.splitlines():
        if "Duration" in line:
            hh, mm, ss = line.split("Duration:")[1].split(",")[0].strip().split(":")
            dur = int(hh) * 3600 + int(mm) * 60 + float(ss)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},fps={FPS},setsar=1")
    cmd = [ff, "-y"]
    loop = False
    if dur >= need + 1:
        start = random.uniform(0, min(dur - need, max(dur - 12, 0)))
        cmd += ["-ss", f"{start:.2f}", "-t", f"{need:.3f}", "-i", raw]
    else:
        start = 0.0
        loop = True
        cmd += ["-stream_loop", "-1", "-t", f"{need:.3f}", "-i", raw]
    cmd += ["-vf", vf, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", out_mp4]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    return {"file": out_mp4, "start": round(start, 2), "loop": loop}
