# -*- coding: utf-8 -*-
from pathlib import Path

import pytest
import requests

from src import material as M


def _vid(vid, dur=30, landscape=True, files=True):
    return {"id": vid,
            "width": 1920 if landscape else 1080,
            "height": 1080 if landscape else 1920,
            "duration": dur,
            "user": {"name": f"author-{vid}"},
            "video_files": ([{"file_type": "video/mp4", "height": 1080,
                              "link": f"http://x/{vid}.mp4"}] if files else [])}


# ── 查询改写阶梯 ────────────────────────────────────────
def test_ladder_order_and_dedupe():
    lad = M.rewrite_ladder("great wall aerial sunrise")
    assert lad[0] == "great wall aerial sunrise"
    assert "great wall aerial" in lad           # 去尾词放宽
    assert "china sunrise" in lad               # china + 核心词
    assert lad[-1] == "china city scenery"      # 通用兜底殿后
    assert len(set(lad)) == len(lad)


def test_ladder_uses_topic_words():
    lad = M.rewrite_ladder("aerial sunrise", topic="The Great Wall: Wonder")
    assert any("great" in q and "sunrise" in q for q in lad)


def test_ladder_short_query_has_no_broaden_rung():
    lad = M.rewrite_ladder("pandas eating")
    assert "pandas eating" in lad               # 不足 3 词 → 无去尾词档
    assert lad[-1] == "china city scenery"


# ── 候选挑选 ────────────────────────────────────────────
def test_pick_filters_portrait_and_used():
    vids = [_vid(1), _vid(2, landscape=False), _vid(3)]
    got = M.pick(vids, 5.0, {3})
    assert got is not None and got["asset_id"] == 1
    assert got["author"] == "author-1"          # 作者信息进入来源记录（评审#5）
    assert got["source_url"].startswith("https://www.pexels.com/video/1")


def test_pick_prefers_duration_enough():
    vids = [_vid(1, dur=3), _vid(2, dur=10)]
    got = M.pick(vids, 5.0, set())
    assert got["asset_id"] == 2                 # 时长不够的段落不选


def test_pick_takes_longest_when_none_enough():
    vids = [_vid(1, dur=3), _vid(2, dur=6)]
    got = M.pick(vids, 30.0, set())
    assert got["asset_id"] == 2


def test_pick_avoids_recent_ids():
    vids = [_vid(1), _vid(2)]
    got = M.pick(vids, 5.0, set(), recent_ids=frozenset({1}))
    assert got["asset_id"] == 2


def test_pick_all_used_returns_none():
    assert M.pick([_vid(1)], 5.0, {1}) is None


def test_pick_no_mp4_returns_none():
    assert M.pick([_vid(1, files=False)], 5.0, set()) is None


# ── 搜索编排（阶梯推进 / 失败分类） ──────────────────────
def test_search_footage_walks_ladder(monkeypatch):
    def fake_search(api_key, kw, per_page=15):
        if kw == "great wall aerial sunrise":
            return {"videos": []}               # 第一档没结果
        if kw == "great wall aerial":
            return {"videos": [_vid(9)]}        # 第二档命中
        return {"videos": []}
    monkeypatch.setattr(M, "search", fake_search)
    rec = M.search_footage("k", "great wall aerial sunrise", 5.0, set())
    assert rec["ladder_step"] == 1
    assert rec["query_used"] == "great wall aerial"
    assert not rec["fallback"]
    assert rec["license"] and rec["source_url"]


def test_search_footage_fallback_is_flagged(monkeypatch):
    def only_fallback(api_key, kw, per_page=15):
        if kw == "china city scenery":
            return {"videos": [_vid(5)]}        # 只有通用兜底档有结果
        return {"videos": []}
    monkeypatch.setattr(M, "search", only_fallback)
    rec = M.search_footage("k", "some odd query xyz", 5.0, set())
    assert rec["fallback"] is True              # 兜底命中必须打标
    assert rec["ladder_step"] > 0


def test_search_footage_exhausted_returns_none(monkeypatch):
    monkeypatch.setattr(M, "search", lambda *a, **k: {"videos": []})
    assert M.search_footage("k", "great wall", 5.0, set()) is None


def test_search_footage_auth_error_propagates(monkeypatch):
    def boom(*a, **k):
        raise M.MaterialError("auth", "401")
    monkeypatch.setattr(M, "search", boom)
    with pytest.raises(M.MaterialError):
        M.search_footage("k", "q", 5.0, set())   # 换词没有意义，直接上抛


def test_search_error_classification(monkeypatch):
    monkeypatch.setattr(M.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("down")))
    with pytest.raises(M.MaterialError) as ei:
        M.search("k", "q")
    assert ei.value.kind == "network"

    class Resp:
        status_code = 401
    monkeypatch.setattr(M.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(M.MaterialError) as ei2:
        M.search("k", "q")
    assert ei2.value.kind == "auth"


# ── 全局下载缓存 ────────────────────────────────────────
class FakeDownload:
    def __init__(self, chunks):
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield from self.chunks


def test_download_cached_hits_second_time(tmp_path, monkeypatch):
    monkeypatch.setattr(
        M.requests, "get",
        lambda link, stream=True, timeout=300: FakeDownload([b"abc", b"def"]))
    p1, hit1 = M.download_cached("http://x/1.mp4", tmp_path, asset_id=7)
    assert not hit1 and Path(p1).read_bytes() == b"abcdef"
    meta = (tmp_path / "pexels_7.json").read_text(encoding="utf-8")
    assert "link" in meta
    p2, hit2 = M.download_cached("http://x/1.mp4", tmp_path, asset_id=7)
    assert hit2 and p2 == p1                    # 第二次直接命中缓存


def test_recent_ids_ring(tmp_path):
    f = tmp_path / "recent.json"
    M.remember_ids(f, [1, 2, 3])
    M.remember_ids(f, [4])
    assert M.load_recent_ids(f) == {1, 2, 3, 4}
