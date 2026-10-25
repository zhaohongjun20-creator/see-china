# -*- coding: utf-8 -*-
"""稳定缓存键（P2-2 分级缓存的基础设施）。

对任意可 JSON 序列化对象生成确定性 sha256 指纹：
- config_hash  —— 影响成片的配置子集指纹；变化后旧任务不续跑、缓存失效
- script hash  —— 讲稿内容指纹，下游（配音/素材/片段）据此判断是否需要重做
- narration hash —— 单段配音指纹（文本 + 音色 + 语速 + 后端 + 脚本指纹）
- compose hash —— 合成参数指纹（片段清单 + BGM + 音量 + 转场 + 字幕）

规则：只把"真正影响结果"的参数放进 key；阈值类配置（如质检阈值）不放。
"""
import hashlib
import json


def stable_hash(obj) -> str:
    """确定性 16 位十六进制指纹；同内容跨进程/跨次运行稳定。"""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def script_fingerprint(doc: dict) -> str:
    """讲稿内容指纹（标题+分片文本+取材词），用于下游缓存失效判断。"""
    secs = [{"text": s.get("text", ""), "video_query": s.get("video_query", "")}
            for s in doc.get("sections", [])]
    return stable_hash({"title": doc.get("title", ""), "sections": secs})


def narration_fingerprint(text: str, voice: str, rate: str, backend: str,
                          script_hash: str) -> str:
    return stable_hash({"text": text, "voice": voice, "rate": rate,
                        "backend": backend, "script": script_hash})


def clip_fingerprint(need: float, width: int, height: int, fps: int,
                     material_file_hash: str) -> str:
    return stable_hash({"need": round(float(need), 3), "w": width, "h": height,
                        "fps": fps, "material": material_file_hash})


def compose_fingerprint(clips: list, bgm_name: str, bgm_volume: float,
                       fade: float, transitions: list, srt_hash: str) -> str:
    return stable_hash({"clips": [str(c) for c in clips], "bgm": bgm_name,
                        "vol": float(bgm_volume), "fade": float(fade),
                        "trans": list(transitions or []), "srt": srt_hash})


def config_fingerprint(cfg: dict, prompt_version: str) -> str:
    """只取影响成片内容的配置子集。

    注意：topics 列表（选题池）不参与 —— 池子里加题不应让进行中任务失效；
    选题是否做过由 store 状态机管理，与任务缓存无关。
    """
    subset = {
        "video": cfg.get("video", {}),
        "transition": cfg.get("transition", {}),
        "narrator": {k: cfg.get("narrator", {}).get(k)
                     for k in ("backend", "voice", "rate")},
        "copywriter": cfg.get("copywriter", {}),
        "bgm_volume": cfg.get("bgm_volume"),
        "hashtags": cfg.get("hashtags", []),
        "prompt_version": prompt_version,
    }
    return stable_hash(subset)


def file_hash(path) -> str | None:
    """文件内容 sha256 前 16 位；文件不存在返回 None。"""
    import pathlib
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
