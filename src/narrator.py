"""配音模块：可插拔多后端 —— edge-tts（免费）| 智谱 cogtts | CosyVoice2 批量 | Piper 本地。

质量优先级：cosy.enable 时 CosyVoice2 本地大模型批量合成接管（质量第一梯队），
失败降级逐段链路：zhipu → edge → 音色熔断池 → Piper 本地神经（免费离线）→
SAPI（仅 allow_sapi 手动模式）。质量闸门：全部神经后端不可用时直接抛错停线，
绝不放 SAPI 机械音上线。
"""
import asyncio, json, os, subprocess, tempfile
from pathlib import Path
import edge_tts
import requests
import imageio_ffmpeg

ZHIPU_SPEECH_URL = "https://open.bigmodel.cn/api/paas/v4/audio/speech"

# 音色熔断池：指定音色合成失败（如端点封堵）时按序降级，保产线不断
EDGE_FALLBACK_VOICES = ["en-US-GuyNeural", "en-US-AriaNeural", "en-GB-RyanNeural"]

# 本地 SAPI 兜底语音（edge 封堵 + Piper 不可用时的终极兜底；离线可用，音质机械）
SAPI_VOICE = "Microsoft Zira Desktop"

# Piper 本地神经音色（免费离线；英文模型 en_US-ryan，模型在 ../tts_models）
PIPER_MODEL = str(Path(__file__).resolve().parent.parent.parent / "tts_models"
                  / "en_US-ryan-medium.onnx")
_piper_voice = None

# CosyVoice2 本地大模型（Apache-2.0，质量第一梯队；环境在 ../cosyvoice，
# 与 bili-history 共享；批量清单用独立文件名，避免两个产线互相覆盖）
_COSY_ROOT = Path(__file__).resolve().parent.parent.parent / "cosyvoice"
_COSY_PY = _COSY_ROOT / ".venv" / "Scripts" / "python.exe"
_COSY_CLI = _COSY_ROOT / "synth_cli.py"
_COSY_MANIFEST = _COSY_ROOT / "_jobs_seechina.json"


def synth_cosy_batch(sections: list, out_dir: str, cosy: dict,
                     indices: list | None = None) -> list:
    """CosyVoice2 批量合成（子进程一次加载模型，全部段落出片后返回 wav 路径）。

    indices：sections 对应的原段落号（断点续跑只补缺段时传，文件名按原段号落盘）。
    任一段失败即抛错，由调用方降级到逐段链路。配方（cosy 配置里选）：
    - mode=instruct2 + prompt_wav + instruct: 参考音频 + 英文纪录片风格指令
    - mode=zero_shot + spk: 预注册音色 ID
    """
    if not (_COSY_PY.exists() and _COSY_CLI.exists()):
        raise RuntimeError(f"cosyvoice env missing: {_COSY_PY}")
    if cosy.get("mode", "zero_shot") == "instruct2":
        base = {"mode": "instruct2",
                "prompt_wav": str(_COSY_ROOT / cosy["prompt_wav"]),
                "instruct": cosy.get("instruct", "")}
    else:
        base = {"spk_id": cosy.get("spk", "")}
    idxs = indices if indices is not None else list(range(len(sections)))
    jobs = [{**base, "text": s["text"],
             "out": str((Path(out_dir) / f"sec_{i:02d}.wav").resolve())}
            for i, s in zip(idxs, sections)]  # 必须绝对路径：子进程 cwd 在 cosyvoice 目录
    _COSY_MANIFEST.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False),
                              encoding="utf-8")
    r = subprocess.run([str(_COSY_PY), str(_COSY_CLI), str(_COSY_MANIFEST)],
                       capture_output=True, text=True, timeout=3600,
                       cwd=str(_COSY_ROOT))
    outs = [j["out"] for j in jobs]
    missing = [o for o in outs if not Path(o).exists()]
    if missing or r.returncode != 0:
        raise RuntimeError(f"cosy batch incomplete: missing={missing} "
                           f"err={r.stderr.strip()[:200]}")
    return outs


def cosy_env_ready() -> bool:
    return _COSY_PY.exists() and _COSY_CLI.exists()


def _synth_piper(text: str, out_path: str) -> str:
    """Piper 本地神经合成（wav）。模型进程内缓存，多次调用零加载开销。"""
    global _piper_voice
    import wave as _wave
    if _piper_voice is None:
        from piper import PiperVoice
        _piper_voice = PiperVoice.load(PIPER_MODEL)
    wav = str(Path(out_path).with_suffix(".wav"))
    with _wave.open(wav, "wb") as w:
        _piper_voice.synthesize_wav(text, w)
    if not Path(wav).exists() or Path(wav).stat().st_size < 5000:
        raise RuntimeError("piper produced empty audio")
    return wav


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
                  allow_sapi: bool = False) -> tuple:
    """按后端合成；zhipu 失败降级 edge；edge 指定音色失败自动换熔断池音色。

    返回 (音频路径, 实际使用的后端, 实际使用的音色) —— 供上层如实记录到
    manifest（评审要求 #6：配音降级必须留痕，SAPI 机械音强制人工审核）。

    质量闸门：神经音色全灭时默认直接抛错（今日不发，等恢复），
    绝不放 SAPI 机械音上线——"产线可以断，烂片不能发"。
    allow_sapi=True 时才允许本地 SAPI 兜底（仅供手动救急，成片会标记待人工审核）。
    """
    if backend == "zhipu" and zhipu and zhipu.get("api_key"):
        try:
            _synth_zhipu(text, out_path, zhipu["api_key"],
                         zhipu.get("model", "cogtts"),
                         zhipu.get("voice", "tongtong"))
            return out_path, "zhipu", zhipu.get("voice", "tongtong")
        except Exception as e:
            print(f"[narrator] zhipu TTS 失败，降级 edge: {e}")
    try:
        _synth_edge(text, out_path, voice, rate)
        return out_path, "edge", voice
    except Exception as e:
        for fb in EDGE_FALLBACK_VOICES:
            if fb == voice:
                continue
            try:
                _synth_edge(text, out_path, fb, rate)
                print(f"[narrator] 音色 {voice} 失败({type(e).__name__})，已熔断降级 → {fb}")
                return out_path, "edge", fb
            except Exception:
                continue
        # edge 全线失败 → Piper 本地神经音色（免费离线，质量可上线）
        try:
            wav = _synth_piper(text, out_path)
            print(f"[narrator] edge-tts 不可用，Piper 本地神经音色接管 → {wav}")
            return wav, "piper", Path(PIPER_MODEL).stem
        except Exception as ep:
            if allow_sapi:
                try:
                    wav = _synth_sapi(text, out_path)
                    print(f"[narrator] 质量闸门放行(手动模式)：本地 SAPI 兜底 → {wav}")
                    return wav, "sapi", SAPI_VOICE
                except Exception as e2:
                    raise RuntimeError(f"edge/Piper/SAPI 均失败: {e2}") from e2
            raise RuntimeError(
                f"edge 与 Piper 均不可用（{type(ep).__name__}）。质量闸门：今日不发布，"
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
    """逐段配音；返回 [{path, duration, text, backend, voice}]。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results = []
    for i, sec in enumerate(sections):
        mp3 = f"{out_dir}/sec_{i:02d}.mp3"
        actual, used_backend, used_voice = synth_section(
            sec["text"], mp3, voice, rate, backend=backend,
            zhipu=zhipu, allow_sapi=allow_sapi)
        results.append({"path": actual, "duration": audio_duration(actual),
                        "text": sec["text"], "backend": used_backend,
                        "voice": used_voice})
    return results


# ── 真实可用性探针（doctor 用；不消耗任何付费额度） ──────
def probe_edge(timeout_s: float = 25) -> str:
    """真实探测 edge-tts。返回 ok | blocked403(微软封堵) | network | error:<类型>"""
    async def _run():
        out = str(Path(tempfile.gettempdir()) / "_seechina_edge_probe.mp3")
        await asyncio.wait_for(
            edge_tts.Communicate("ok", EDGE_FALLBACK_VOICES[0]).save(out),
            timeout=timeout_s)
    try:
        asyncio.run(_run())
        return "ok"
    except Exception as e:
        if "403" in str(e):
            return "blocked403"
        name = type(e).__name__
        if "Timeout" in name or "Connection" in name or "SSL" in name \
                or "Connection" in str(e):
            return "network"
        return f"error:{name}"


def probe_piper() -> str:
    """真实探测 Piper 本地合成。返回 ok | missing:package | missing:model | error:<类型>"""
    try:
        import wave
        from piper import PiperVoice
    except ImportError:
        return "missing:package"
    if not Path(PIPER_MODEL).is_file():
        return "missing:model"
    try:
        v = PiperVoice.load(PIPER_MODEL)
        out = str(Path(tempfile.gettempdir()) / "_seechina_piper_probe.wav")
        with wave.open(out, "wb") as w:
            v.synthesize_wav("ok", w)
        return "ok"
    except Exception as e:
        return f"error:{type(e).__name__}"
