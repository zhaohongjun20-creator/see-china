# -*- coding: utf-8 -*-
from src import jobman

CFG = {"a": 1}


def test_new_job_structure(tmp_path):
    j = jobman.new_job(tmp_path, "The Great Wall", CFG, "hash1", "pv1")
    assert (j.dir / "manifest.json").is_file()
    assert j.narration_dir.is_dir() and j.materials_dir.is_dir()
    assert j.clips_dir.is_dir() and j.logs_dir.is_dir()
    mf = j.manifest.data
    assert mf["topic"] == "The Great Wall"
    assert mf["config_hash"] == "hash1"
    assert mf["prompt_version"] == "pv1"


def test_find_resumable(tmp_path):
    j = jobman.new_job(tmp_path, "The Great Wall", CFG, "hash1")
    again = jobman.find_resumable(tmp_path, "The Great Wall", "hash1")
    assert again is not None and again.dir == j.dir
    assert jobman.find_resumable(tmp_path, "Other Topic", "hash1") is None
    assert jobman.find_resumable(tmp_path, "The Great Wall", "hash2") is None


def test_packaged_job_not_resumable(tmp_path):
    j = jobman.new_job(tmp_path, "T", CFG, "h")
    j.manifest.stage_done("package")
    assert jobman.find_resumable(tmp_path, "T", "h") is None


def test_stage_lifecycle_and_first_unfinished(tmp_path):
    j = jobman.new_job(tmp_path, "T", CFG, "h")
    m = j.manifest
    assert m.first_unfinished() == "script"
    m.stage_start("script")
    assert m.stage("script")["attempts"] == 1
    m.stage_fail("script", "boom")
    assert m.stage_status("script") == "failed"
    assert m.stage("script")["error"] == "boom"
    m.stage_start("script")                   # 重试计入 attempts
    assert m.stage("script")["attempts"] == 2
    m.stage_done("script", duration_s=1.234)
    assert m.stage_status("script") == "done"
    assert m.stage("script")["duration_s"] == 1.23
    assert m.first_unfinished() == "narration"


def test_section_records(tmp_path):
    j = jobman.new_job(tmp_path, "T", CFG, "h")
    j.manifest.set_section(0, "narration", {"hash": "x", "duration": 1.5})
    j.manifest.set_section(0, "material", {"asset_id": 9})
    assert j.manifest.section(0)["narration"]["hash"] == "x"
    assert j.manifest.section(0)["material"]["asset_id"] == 9


def test_manifest_survives_reload(tmp_path):
    j = jobman.new_job(tmp_path, "T", CFG, "h")
    j.manifest.set("script_hash", "abc")
    loaded = jobman.load_job(tmp_path, j.dir.name)
    assert loaded.manifest.data["script_hash"] == "abc"


def test_log_event(tmp_path):
    j = jobman.new_job(tmp_path, "T", CFG, "h")
    jobman.log_event(j, "test_event", k=1)
    jobman.log_event(j, "test_event", k=2)
    lines = (j.logs_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"test_event"' in lines[-1]
