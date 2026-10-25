"""配音模块：可插拔双后端 —— edge-tts（免费）| 智谱 cogtts（大模型，更自然）。

zhipu 后端失败（余额不足/网络）时自动降级到 edge，保证产线不断。
"""
import asyncio, os, subprocess
from pathlib import Path
import edge_tts
import requests
import imageio_ffmpeg

ZHIPU_SPEECH_URL = "https://open.bigmodel.cn/api/paas/v4/audio/speech"

# 音色熔断池：指定音色合成失败（如云泽在免费端点不可用）时按序降级，保产线不断
EDGE_FALLBACK_VOICES = ["en-US-GuyNeural", "en-US-AriaNeural", "en-GB-RyanNeural"]

# 本地 SAPI 兜底语音（微软封堵 edge-tts 免费端点时启用；离线可用，音质略机械）
SAPI_VOICE = "Microsoft Zira Desktop"


def _synth_sapi(text: str, out_path: str) -> str:
    """Windows 本地语音合成（System.Speech，wav 格式），edge 全线失败时的终极兜底。"""
    wav = str(Path(out_path).with_suffix(".wav"))
    ps = ("Add-Type -AssemblyName System.Speech;"
          f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SelectVoice('{SAPI_VOICE}'); $s.Rate = 0;"
          f"$s.SetOutputToWaveFile('{wav}');"
          "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       input=text, text=True, capture_output=True, timeout=300)
    if not Path(wav).exists() or Path(wav).stat().st_size < 5000:
        raise RuntimeError(f"SAPI failed: {r.stderr[:150]}")
    return wav


def _synth_edge(text: str, out_path: str, voice: str, rate: str) -> str:
    async def run():
        await edge_tts.Communicate(text, voice, rate=rate).save(out_path)
    asyncio.run(run())
    return out_path


def _synth_zhipu(text: str, out_path: str, api_key: str, model: str, voice: str) -> str:
    r = requests.post(
        ZHIPU_SPEECH_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": text, "voice": voice},
        timeout=120,
    )
    if r.status_code != 200 or len(r.content) < 2000:
        raise RuntimeError(f"zhipu tts failed: {r.status_code} {r.text[:120]}")
    Path(out_path).write_bytes(r.content)
    return out_path


def synth_section(text: str, out_path: str, voice: str, rate: str,
                  backend: str = "edge", zhipu: dict | None = None,
                  allow_sapi: bool = False) -> str:
    """按后端合成；zhipu 失败降级 edge；edge 指定音色失败自动换熔断池音色。

    质量闸门：神经音色全灭时默认直接抛错（今日不发，等恢复），
    绝不放 SAPI 机械音上线——"产线可以断，烂片不能发"。
    allow_sapi=True 时才允许本地 SAPI 兜底（仅供手动救急）。
    """
    if backend == "zhipu" and zhipu and zhipu.get("api_key"):
        try:
            return _synth_zhipu(text, out_path, zhipu["api_key"],
                                zhipu.get("model", "cogtts"),
                                zhipu.get("voice", "tongtong"))
        except Exception as e:
            print(f"[narrator] zhipu TTS 失败，降级 edge: {e}")
    try:
        return _synth_edge(text, out_path, voice, rate)
    except Exception as e:
        for fb in EDGE_FALLBACK_VOICES:
            if fb == voice:
                continue
            try:
                _synth_edge(text, out_path, fb, rate)
                print(f"[narrator] 音色 {voice} 失败({type(e).__name__})，已熔断降级 → {fb}")
                return out_path
            except Exception:
                continue
        if allow_sapi:
            try:
                wav = _synth_sapi(text, out_path)
                print(f"[narrator] 质量闸门放行(手动模式)：本地 SAPI 兜底 → {wav}")
                return wav
            except Exception as e2:
                raise RuntimeError(f"edge 与 SAPI 均失败: {e2}")
        raise RuntimeError(
            "edge-tts 神经音色全部不可用（微软端封堵中）。质量闸门：今日不发布，"
            "等恢复后自动继续；拒绝 SAPI 机械音上线。")


def audio_duration(path: str) -> float:
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", path],
                       capture_output=True, text=True, timeout=60)
    m = next((l for l in r.stderr.splitlines() if "Duration" in l), "")
    try:
        hh, mm, ss = m.split("Duration:")[1].split(",")[0].strip().split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (ValueError, IndexError):
        return 0.0


def narrate(sections: list, out_dir: str, voice: str, rate: str,
            backend: str = "edge", zhipu: dict | None = None,
            allow_sapi: bool = False) -> list:
    """逐段配音；返回 [{path, duration, text}]。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results = []
    for i, sec in enumerate(sections):
        mp3 = f"{out_dir}/sec_{i:02d}.mp3"
        actual = synth_section(sec["text"], mp3, voice, rate, backend=backend,
                               zhipu=zhipu, allow_sapi=allow_sapi)
        results.append({"path": actual, "duration": audio_duration(actual),
                        "text": sec["text"]})
    return results
