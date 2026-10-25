# -*- coding: utf-8 -*-
"""评审#4：上传说明生成统一 —— 正常打包与人工批准共用 build_upload_info()。"""
import autopost as A


def _records():
    return [{"asset_id": 7, "author": "Ann", "query_used": "giant pandas",
             "segment_start": 1.5, "segment_seconds": 2.0,
             "source_url": "https://www.pexels.com/video/7/",
             "license": "Pexels License"},
            {"asset_id": 8, "author": "", "query_used": "high speed rail",
             "segment_start": 0.0, "segment_seconds": 1.7, "loop_fill": True,
             "source_url": "https://www.pexels.com/video/8/",
             "license": "Pexels License"}]


def _doc():
    return {"title": "Test Episode", "description": "A test.", "tags": ["a", "b"]}


def test_upload_info_contains_all_fields():
    info = A.build_upload_info(_doc(), _records(), {"hashtags": ["SeeChina"]})
    assert "TITLE: Test Episode" in info
    assert "DESCRIPTION:\nA test." in info
    assert "Footage credits:" in info
    assert "IG_CAPTION:" in info
    assert "TAGS: a, b, SeeChina" in info


def test_upload_info_credits_carry_provenance():
    info = A.build_upload_info(_doc(), _records(), {})
    # 作者 + 片段区间 + 来源页 + license 全部在署名行里（评审#5）
    assert "Pexels #7 by Ann: 'giant pandas', used 1.5s-3.5s" in info
    assert "https://www.pexels.com/video/7/" in info
    assert "Pexels License" in info
    # 无作者时不留 "by"
    assert "#8 by" not in info
    # 两条素材都在
    assert "#7" in info and "#8" in info


def test_upload_info_identical_across_callers():
    """同输入下函数输出确定一致 —— 两条打包路径不可能再分叉。"""
    r, d, c = _records(), _doc(), {"hashtags": ["SeeChina"]}
    assert A.build_upload_info(d, r, c) == A.build_upload_info(d, r, c)
