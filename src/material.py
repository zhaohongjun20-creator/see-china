# -*- coding: utf-8 -*-
"""Pexels 实拍素材（P1-5 强化：来源可追溯 + 查询改写阶梯 + 全局下载缓存）。

search_footage():  按段讲稿的 video_query 搜素材；失败时沿"改写阶梯"换词重试
                   （原词 → 放宽 → 主题词组合 → china+核心词 → 通用兜底），
                   每一步都记录用到的词与阶梯层级，绝不静默退化。
download_cached(): 全局素材缓存（data/material_cache/），同一素材跨集复用不再下载。
每条素材都带 asset_id / source_url / license 信息，写入任务素材清单供追溯。
"""
import datetime
import json
import os
import random
import re
import time
from pathlib import Path

import requests

from .cachekeys import stable_hash

PEXELS_SEARCH = "https://api.pexels.com/videos/search"
PEXELS_LICENSE = "Pexels License (free, no attribution required)"


class MaterialError(Exception):
    """素材检索失败。kind: network | auth | empty —— 上游据此决定重试或报错。"""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"[{kind}] {message}")


def search(api_key: str, keyword: str, per_page: int = 15) -> dict:
    try:
        r = requests.get(
            PEXELS_SEARCH,
            headers={"Authorization": api_key},
            params={"query": keyword, "per_page": per_page, "orientation": "landscape",
                    "size": "medium"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise MaterialError("network", f"{type(e).__name__}: {e}") from e
    if r.status_code in (401, 403):
        raise MaterialError("auth", f"pexels auth failed http {r.status_code}")
    if r.status_code == 429:
        raise MaterialError("network", "pexels rate limited (429)")
    r.raise_for_status()
    return r.json()


def pick(videos: list, need: float, used_ids: set,
         recent_ids: frozenset = frozenset()) -> dict | None:
    """从候选里挑一条：横屏、本集未用过、优先近期未用过、时长足够。

    返回 {asset_id, link, duration, width, height} 或 None。
    """
    cands = [v for v in videos
             if v.get("width", 0) > v.get("height", 0) and v["id"] not in used_ids]
    if not cands:
        return None
    prefer = [v for v in cands if v["id"] not in recent_ids] or cands
    enough = [v for v in prefer if v.get("duration", 0) >= need]
    v = random.choice(enough) if enough else max(prefer, key=lambda x: x.get("duration", 0))
    mp4s = [f for f in v.get("video_files", [])
            if f.get("file_type") == "video/mp4" and f.get("height", 0) <= 1080]
    if not mp4s:
        return None
    best = max(mp4s, key=lambda f: f.get("height", 0))
    return {"asset_id": v["id"], "link": best["link"],
            "duration": v.get("duration", 0),
            "width": v.get("width"), "height": v.get("height"),
            "author": (v.get("user") or {}).get("name", ""),
            "source_url": f"https://www.pexels.com/video/{v['id']}/"}


def rewrite_ladder(query: str, topic: str = "") -> list:
    """查询改写阶梯（确定性，可测试，不花 token）。

    原词最准 → 去掉末词放宽 → 主题核心词+段落核心词 → china+核心词 → 通用兜底。
    通用兜底排在最后且被标记 fallback=True，绝不会冒充精准命中。
    """
    q = re.sub(r"\s+", " ", (query or "").strip())
    words = q.split(" ")
    core = words[-1] if words else q
    ladder = [q] if q else []
    if len(words) > 2:
        ladder.append(" ".join(words[:-1]))
    if topic:
        twords = [w for w in re.findall(r"[A-Za-z]{3,}", topic)][:2]
        if twords:
            ladder.append(f"{' '.join(twords).lower()} {core}")
    if core.lower() != "china":
        ladder.append(f"china {core.lower()}")
    ladder.append("china city scenery")
    out, seen = [], set()
    for c in ladder:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def search_footage(api_key: str, query: str, need: float, used_ids: set,
                   topic: str = "", recent_ids: frozenset = frozenset()
                   ) -> dict | None:
    """沿改写阶梯搜索，返回素材记录 dict（含来源追溯字段）或 None。

    网络/认证错误直接上抛（换词没有意义，由上层决定重试策略）；
    每一档都搜不到才返回 None —— 上层必须显式处理，不许拿无关素材冒充。
    """
    ladder = rewrite_ladder(query, topic)
    last_err = None
    for step, q in enumerate(ladder):
        try:
            res = search(api_key, q)
        except MaterialError as e:
            if e.kind in ("network", "auth"):
                raise
            last_err = e
            continue
        got = pick(res.get("videos", []), need, used_ids, recent_ids)
        if got:
            return {
                "query": query, "query_used": q, "ladder_step": step,
                "provider": "pexels", "license": PEXELS_LICENSE,
                "asset_id": got["asset_id"], "source_url": got["source_url"],
                "author": got.get("author", ""),
                "link": got["link"], "duration": got["duration"],
                "fallback": step == len(ladder) - 1 and q == "china city scenery",
                "selected_reason": ("matches narration query"
                                    if step == 0 else
                                    f"rewritten query (ladder step {step}: '{q}')"),
            }
    if last_err:
        raise last_err
    return None


# ── 全局下载缓存：同一素材跨集复用，不再重复下载 ──────────
def download_cached(link: str, cache_dir, asset_id=None) -> tuple:
    """下载到全局缓存（已存在则直接命中）。返回 (本地路径, 是否命中缓存)。"""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    name = f"pexels_{asset_id}" if asset_id else f"pexels_{stable_hash(link)}"
    dest, meta = cache / f"{name}.mp4", cache / f"{name}.json"
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest), True
    part = dest.with_suffix(".part")
    with requests.get(link, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    os.replace(part, dest)
    meta.write_text(json.dumps({
        "link": link, "asset_id": asset_id, "fetched_at":
            datetime.datetime.now().isoformat(timespec="seconds"),
        "bytes": dest.stat().st_size}), encoding="utf-8")
    return str(dest), False


# ── 近期用过素材记录：跨集避免同一镜头反复出现 ────────────
def load_recent_ids(path, keep: int = 80) -> set:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return set(data[-keep:])
    except Exception:
        return set()


def remember_ids(path, ids, keep: int = 80):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        data = []
    data = (data + list(ids))[-keep:]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def wait_retry(seconds: float):
    """限流退避（网络 429 时由上层调用）；独立函数便于测试时替换。"""
    time.sleep(seconds)
