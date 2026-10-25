# -*- coding: utf-8 -*-
"""Pexels 实拍素材（MoneyPrinterTurbo 方法论：每段讲稿配真实视频片段）。

search_best(): 按关键词找横屏 HD 片段，优先时长足够的；返回直链与时长。
fetch_clip():  下载 + 去重（本集内不重复用同一素材）。
"""
import os, random, requests

PEXELS_SEARCH = "https://api.pexels.com/videos/search"


def search(api_key: str, keyword: str, per_page: int = 15) -> dict:
    r = requests.get(
        PEXELS_SEARCH,
        headers={"Authorization": api_key},
        params={"query": keyword, "per_page": per_page, "orientation": "landscape",
                "size": "medium"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def search_best(api_key: str, keyword: str, need: float,
                used_ids: set) -> tuple[str, int] | None:
    """找一条时长≥need 的横屏素材（找不到就取最长的），返回 (下载直链, 原时长秒)。"""
    try:
        res = search(api_key, keyword)
    except Exception:
        return None
    cands = [v for v in res.get("videos", [])
             if v["width"] > v["height"] and v["id"] not in used_ids]
    if not cands:
        return None
    enough = [v for v in cands if v["duration"] >= need]
    v = random.choice(enough) if enough else max(cands, key=lambda x: x["duration"])
    mp4s = [f for f in v["video_files"]
            if f.get("file_type") == "video/mp4" and f.get("height", 0) <= 1080]
    if not mp4s:
        return None
    best = max(mp4s, key=lambda f: f.get("height", 0))
    used_ids.add(v["id"])
    return best["link"], v["duration"]


def download(link: str, dest: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with requests.get(link, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest
