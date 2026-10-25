"""幻灯片成片：史料图四向 Ken Burns + 旁白逐段对齐 + 交叉溶解转场 + BGM + 字幕。

转场音画对齐原理：每段视频（除末段）尾部多留 fade 秒画面，旁白说完后画面
停留溶解切换（纪录片节奏）；音频 concat 后总长恰与 xfade 后视频总长相等。
"""
import subprocess
from pathlib import Path
import imageio_ffmpeg

W, H, FPS = 1920, 1080, 30
BIG = 2400  # 先放大再 zoom，减轻整数抖动


def _kenburns(mode: str, frames: int) -> str:
    """四向运镜：in 推近 / out 拉远 / panleft 左移 / panright 右移。"""
    center = "iw/2-(iw/zoom/2)"
    vcenter = "ih/2-(ih/zoom/2)"
    if mode == "in":
        return f"z='min(1.0+0.0006*on,1.12)':x='{center}':y='{vcenter}'"
    if mode == "out":
        return f"z='max(1.12-0.0006*on,1.0)':x='{center}':y='{vcenter}'"
    if mode == "panright":
        return (f"z='1.10':x='(iw-iw/zoom)*on/{frames}':y='{vcenter}'")
    return f"z='1.10':x='(iw-iw/zoom)*(1-on/{frames})':y='{vcenter}'"


def build_section_clip(img: str, voice_mp3: str, duration: float, out_mp4: str,
                       mode: str = "in", stickers: list = None) -> str:
    """史料图 Ken Burns + 旁白 +（可选）Twemoji 表情贴纸 overlay。"""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    frames = max(2, int(duration * FPS))
    vf = (f"scale={BIG}:{int(BIG*H/W)}:force_original_aspect_ratio=increase,"
          f"crop={BIG}:{int(BIG*H/W)},"
          f"zoompan={_kenburns(mode, frames)}"
          f":d=1:s={W}x{H}:fps={FPS},setsar=1")

    extra_inputs, chains, label = [], [], "[base]"
    positions = [(W - 265, H - 400), (W - 450, H - 255)]   # 右侧偏下，避开字幕
    for i, st in enumerate(stickers or []):
        idx = 2 + i  # 输入：0=图 1=旁白 贴纸从2起
        extra_inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", st["path"]]
        x, y = positions[i % len(positions)]
        nxt = f"[s{i}]"
        chains.append(f"{label}[{idx}:v]overlay=x={x}:y={y}:"
                      f"enable='between(t,{st['at']:.2f},{st['at'] + 2.6:.2f})'{nxt}")
        label = nxt

    cmd = [ffmpeg, "-y", "-loop", "1", "-framerate", str(FPS),
           "-t", f"{duration:.3f}", "-i", img,
           "-i", voice_mp3, *extra_inputs]
    if stickers:
        fc = f"[0:v]{vf}[base];" + ";".join(chains)
        cmd += ["-filter_complex", fc, "-map", label, "-map", "1:a"]
    else:
        fc = f"[0:v]{vf}[v]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", out_mp4]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    return out_mp4


def _fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_srt(narrations: list, path: str) -> str:
    """每段旁白一条字幕，时间轴按段起点对齐（含转场偏移）。"""
    lines, t0 = [], 0.0
    for i, n in enumerate(narrations, 1):
        lines += [str(i), f"{_fmt_ts(t0)} --> {_fmt_ts(t0 + n['duration'])}",
                  n["text"].replace("\n", " "), ""]
        t0 += n["duration"] + n.get("tail", 0.0)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return path


# 纪录片字幕样式：白字黑边+轻阴影（抄 MoneyPrinterTurbo 的可配置思路，默认值内置）
SUBTITLE_STYLE = ("FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,"
                  "OutlineColour=&H96000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=30,"
                  "Alignment=2")

# 段落转场轮换池（xfade 纪录片向：溶解/擦除/圆形展开/黑场）
DEFAULT_TRANSITIONS = ["fade", "dissolve", "wipeleft", "circleopen", "fadeblack"]


def compose_final(section_files: list, bgm_path: str, srt_path: str,
                  out_path: str, bgm_volume: float = 0.22,
                  fade: float = 0.6, transitions: list = None,
                  subtitle_style: str = None) -> str:
    """三步合成（一步大 filter 图会因 apad 无限流+多级 xfade 死锁）：
    A. 仅视频：多级 xfade 转场（样式按 transitions 轮换，默认纪录片池）
    B. 仅音频：concat 各段旁白
    C. 收尾：视频字幕烧录 + 旁白/BGM 混音（sidechain 压低 BGM 突出人声）
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    n = len(section_files)
    work = Path(out_path).with_suffix("")
    v_only, a_only = f"{work}_v.mp4", f"{work}_a.m4a"

    # 各段实际时长
    durs = []
    for f in section_files:
        r = subprocess.run([ffmpeg, "-i", f], capture_output=True, text=True, timeout=60)
        line = next((l for l in r.stderr.splitlines() if "Duration" in l), "")
        hh, mm, ss = line.split("Duration:")[1].split(",")[0].strip().split(":")
        durs.append(int(hh) * 3600 + int(mm) * 60 + float(ss))

    # A. 仅视频 xfade（settb 统一时间基，xfade 的硬要求）
    # acc=已合成流总长：offset=acc-fade，合成后 acc=offset+durs[i]（勿重复累加）
    # 转场样式逐 junction 轮换（纪录片常见手法：相邻切换不重样）
    styles = transitions or DEFAULT_TRANSITIONS
    fc_parts, acc, cur = [f"[0:v]settb=AVTB[b0]"], durs[0], "[b0]"
    for i in range(1, n):
        offset = max(0.0, acc - fade)
        style = styles[(i - 1) % len(styles)]
        fc_parts.append(f"[{i}:v]settb=AVTB,format=yuv420p[s{i}]")
        fc_parts.append(f"{cur}[s{i}]xfade=transition={style}:duration={fade}:"
                        f"offset={offset:.3f}[b{i}]")
        cur = f"[b{i}]"
        acc = offset + durs[i]
    fc_a = ";".join(fc_parts)
    cmd_a = [ffmpeg, "-y"]
    for f in section_files:
        cmd_a += ["-i", str(Path(f).resolve())]
    cmd_a += ["-filter_complex", fc_a, "-map", cur, "-an",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p", v_only]
    subprocess.run(cmd_a, check=True, capture_output=True, timeout=3600)

    # B. 仅音频 concat
    fc_b = ("".join(f"[{i}:a]" for i in range(n)) +
            f"concat=n={n}:v=0:a=1[aout]")
    cmd_b = [ffmpeg, "-y"]
    for f in section_files:
        cmd_b += ["-i", str(Path(f).resolve())]
    cmd_b += ["-filter_complex", fc_b, "-map", "[aout]",
              "-c:a", "aac", "-b:a", "192k", a_only]
    subprocess.run(cmd_b, check=True, capture_output=True, timeout=600)

    # C. 收尾：字幕 + BGM 混音（单视频流，轻）
    # BGM ducking：旁白作 sidechain 触发源，说话时 BGM 自动压低（广播级配比），
    # 比固定音量 amix 层次感好得多——bgm_volume 变为"无人声时的铺底基线"
    style = subtitle_style or SUBTITLE_STYLE
    fc_c = (f"[1:a]asplit=2[voice][key];"
            f"[2:a]volume={bgm_volume}[bg];"
            f"[bg][key]sidechaincompress=threshold=0.02:ratio=6:attack=150:release=900[bgd];"
            f"[voice][bgd]amix=inputs=2:duration=first:normalize=0[aout];"
            f"[0:v]subtitles={Path(srt_path).as_posix()}"
            f":force_style='{style}'[vout]")
    cmd_c = [ffmpeg, "-y", "-i", v_only, "-i", a_only,
             "-stream_loop", "-1", "-i", bgm_path,
             "-filter_complex", fc_c,
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest", out_path]
    try:
        subprocess.run(cmd_c, check=True, capture_output=True, timeout=3600)
    finally:
        Path(v_only).unlink(missing_ok=True)
        Path(a_only).unlink(missing_ok=True)
    return out_path
