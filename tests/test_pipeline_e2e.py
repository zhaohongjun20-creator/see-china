# -*- coding: utf-8 -*-
"""端到端管线测试：LLM/TTS/Pexels/审核全部用假实现替换，其余（渲染/合成/质检/
状态机/manifest/缓存）走真实代码，用小样片跑通 script→…→package 全程。"""
import subprocess

import pytest
import imageio_ffmpeg

import autopost as A
from src import jobman
from src.store import Store

FF = lambda: imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope="module")
def raw_footage(tmp_path_factory):
    d = tmp_path_factory.mktemp("raw")
    p = d / "raw.mp4"
    subprocess.run([FF(), "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=1920x1080:rate=30",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True, timeout=300)
    return p


@pytest.fixture(scope="module")
def bgm_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("bgm")
    # BGM 用 WAV 数据 + .mp3 文件名：ffmpeg 按内容识别，避免依赖 mp3 编码器
    p = d / "calm.mp3"
    subprocess.run([FF(), "-y", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=5",
                    "-f", "wav", "-c:a", "pcm_s16le", str(p)],
                   check=True, capture_output=True, timeout=60)
    return d


@pytest.fixture
def env(tmp_path, monkeypatch, raw_footage, bgm_dir):
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setattr(A, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(A, "JOBS_DIR", tmp_path / "data" / "jobs")
    monkeypatch.setattr(A, "LOG_DIR", tmp_path / "logs")

    def fake_generate(api_key, topic, **kw):
        return {"title": "Test Episode", "description": "d", "tags": ["t1"],
                "sections": [{"text": "word " * 30, "video_query": f"test {i}"}
                             for i in range(2)],
                "_usage": {"calls": 1, "real_tokens": 0,
                           "tokens_estimated": True, "chars_in": 10,
                           "chars_out": 20},
                "_warnings": [], "_prompt_version": "test"}
    monkeypatch.setattr(A.script_mod, "generate", fake_generate)

    def fake_synth(text, out_path, voice, rate, backend="edge", zhipu=None,
                   allow_sapi=False):
        subprocess.run([FF(), "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=0.5",
                        "-f", "wav", "-c:a", "pcm_s16le", str(out_path)],
                       check=True, capture_output=True, timeout=60)
        return str(out_path), "edge", voice
    monkeypatch.setattr(A.narrator, "synth_section", fake_synth)

    def fake_search(api_key, query, need, used_ids, topic="", recent_ids=frozenset()):
        return {"query": query, "query_used": query, "ladder_step": 0,
                "provider": "pexels", "license": "Pexels License",
                "asset_id": 100 + len(used_ids), "author": "Test Author",
                "source_url": "https://www.pexels.com/video/1/",
                "link": "http://fake/link", "duration": 30,
                "fallback": False, "selected_reason": "matches narration query"}
    monkeypatch.setattr(A.material, "search_footage", fake_search)

    def fake_download(link, cache_dir, asset_id=None):
        dest = A.DATA_DIR / "material_cache" / f"pexels_{asset_id}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        hit = dest.exists()
        if not hit:
            dest.write_bytes(raw_footage.read_bytes())
        return str(dest), hit
    monkeypatch.setattr(A.material, "download_cached", fake_download)

    def fake_review(api_key, doc, model=None):
        return {"status": "pass", "issues": [], "deterministic_issues": []}
    monkeypatch.setattr(A.review_mod, "run_review", fake_review)

    cfg = {
        "topics": ["Test Topic"],
        "max_retries": 1,
        "video": {"width": 1920, "height": 1080, "fps": 30,
                  "sections_min": 2, "sections_max": 2,
                  "chars_per_section": [10, 60]},
        "script": {"max_fix_attempts": 1},
        "transition": {"fade_seconds": 0.6, "tail_seconds": 0.6,
                       "end_hold_seconds": 1.0,
                       "styles": ["fade", "dissolve"]},
        "narrator": {"backend": "edge", "voice": "en-US-X", "rate": "+0%",
                     "allow_sapi_fallback": False},
        "copywriter": {"model": "m", "temperature": 0.5},
        "review": {"model": "m"},
        "bgm_volume": 0.2,
        "bgm_dir": str(bgm_dir),
        "hashtags": [],
        "qc": {},
    }
    monkeypatch.setattr(A, "load_config", lambda: cfg)
    return cfg


def test_full_pipeline_and_idempotence(env):
    rc = A.run(check_services=False)
    assert rc == 0

    jobs = jobman.all_jobs(A.JOBS_DIR)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.final.is_file(), "final.mp4 should exist"
    assert j.srt.is_file() and j.cover.is_file() and j.upload_info.is_file()
    assert j.materials_index.is_file()
    mf = j.manifest.data
    for st in ("script", "narration", "materials", "clips", "compose",
               "qc", "review", "package"):
        assert mf["stages"][st]["status"] == "done", st
    assert "final" in mf["outputs"]

    # 素材来源清单可追溯（P1-5 + 评审#5：asset_id/来源页/作者/片段区间）
    index_text = j.materials_index.read_text(encoding="utf-8")
    assert "pexels.com" in index_text and "Pexels License" in index_text
    assert "Test Author" in index_text
    assert "segment_start" in index_text and "segment_seconds" in index_text

    # 配音后端/音色留痕（评审#6）
    narr_rec = mf["sections"]["0"]["narration"]
    assert narr_rec["backend_used"] == "edge"
    assert narr_rec["voice"] == "en-US-X"

    # 上传信息包含素材署名（作者+ID+来源页）
    info = j.upload_info.read_text(encoding="utf-8")
    assert "TITLE: Test Episode" in info and "Footage credits:" in info
    assert "Pexels #100 by Test Author" in info

    # 成本账本有记录（P2-1）
    costs_lines = (j.dir / "costs.jsonl").read_text(encoding="utf-8")
    assert "llm" in costs_lines and "tts" in costs_lines

    # 状态机：ready（成片完成 ≠ published）
    store = Store(str(A.DATA_DIR / "topics.db"))
    assert store.topic_row("Test Topic")["status"] == "ready"

    # 二次运行：选题已完成 → 不新建任务、不重复生成（P0-2/P2-2）
    rc2 = A.run(check_services=False)
    assert rc2 == 0
    assert len(jobman.all_jobs(A.JOBS_DIR)) == 1


def test_human_review_flow(env, monkeypatch):
    """审核发现 high 级问题 → 停在 review；人工批准后 → ready。"""
    def flagged_review(api_key, doc, model=None):
        return {"status": "flagged",
                "issues": [{"section": 0, "severity": "high",
                            "issue": "wrong year", "suggestion": "fix",
                            "needs_source": True}],
                "deterministic_issues": []}
    monkeypatch.setattr(A.review_mod, "run_review", flagged_review)
    rc = A.run(check_services=False)
    assert rc == 2                                   # 等待人工，不算崩溃
    jobs = jobman.all_jobs(A.JOBS_DIR)
    j = [x for x in jobs if x.manifest.data["topic"] == "Test Topic"][0]
    store = Store(str(A.DATA_DIR / "topics.db"))
    assert store.topic_row("Test Topic")["status"] == "review"
    assert not j.upload_info.exists()                # 未过审不出上传包

    # 人工批准 → package 补完 → ready
    rc = A.cmd_review_job(j.dir.name, approve=True)
    assert rc == 0
    assert j.upload_info.exists()
    assert store.topic_row("Test Topic")["status"] == "ready"

    # 人工批准路径的上传包与正常打包同构（评审#4）：作者/片段区间/IG 文案齐全
    info = j.upload_info.read_text(encoding="utf-8")
    assert "TITLE: Test Episode" in info
    assert "IG_CAPTION:" in info and "TAGS:" in info
    assert "Pexels #100 by Test Author" in info
    assert "used 0.0s-" in info            # 片段区间来自 clips 阶段的溯源数据
