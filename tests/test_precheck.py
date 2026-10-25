# -*- coding: utf-8 -*-
from src import precheck as P


def base_cfg(tmp_path):
    return {
        "topics": ["T1", "T2"],
        "video": {"width": 1920, "height": 1080, "fps": 30,
                  "sections_min": 2, "sections_max": 4,
                  "chars_per_section": [20, 60]},
        "narrator": {"backend": "edge", "voice": "v", "rate": "+0%"},
        "copywriter": {"model": "m", "temperature": 0.5},
        "bgm_volume": 0.2,
        "transition": {"fade_seconds": 0.6, "styles": ["fade", "dissolve"]},
        "bgm_dir": str(tmp_path / "bgm"),
    }


def test_env_missing_and_placeholder(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.setenv("PEXELS_API_KEY", "your_pexels_key_here")
    rs = P.check_env()
    by_name = {r.name: r for r in rs}
    assert not by_name["env:ZHIPU_API_KEY"].ok
    assert not by_name["env:PEXELS_API_KEY"].ok       # 占位符 = 没配
    assert "sk-secret" not in str(rs)


def test_mask_never_reveals_value():
    assert P.mask("sk-live-abcdef123456") == "****"
    assert P.mask("") == "(empty)"


def test_config_problems_detection():
    bad = {"topics": [],
           "video": {"width": 1920, "height": 1080, "fps": 30,
                     "sections_min": 5, "sections_max": 2,
                     "chars_per_section": [9, 3]},
           "narrator": {"backend": "wat"},
           "copywriter": {},
           "bgm_volume": 7,
           "transition": {"fade_seconds": 99, "styles": ["notreal"]}}
    fields = {f for f, _ in P.config_problems(bad)}
    assert "topics" in fields
    assert "video.sections_min/max" in fields
    assert "video.chars_per_section" in fields
    assert "narrator.backend" in fields
    assert "copywriter.model" in fields
    assert "bgm_volume" in fields
    assert "transition.fade_seconds" in fields
    assert "transition.styles" in fields


def test_config_ok_has_no_problems(tmp_path):
    assert P.config_problems(base_cfg(tmp_path)) == []


def test_full_precheck_ok(tmp_path, monkeypatch):
    cfg = base_cfg(tmp_path)
    (tmp_path / "bgm").mkdir()
    (tmp_path / "bgm" / "a.mp3").write_bytes(b"x")
    monkeypatch.setenv("ZHIPU_API_KEY", "k1")
    monkeypatch.setenv("PEXELS_API_KEY", "k2")
    pc = P.run_precheck(cfg, tmp_path, probe_services=False)
    assert pc.ok, pc.summary()


def test_empty_bgm_dir_blocks_startup(tmp_path, monkeypatch):
    cfg = base_cfg(tmp_path)                 # bgm 目录存在但空
    (tmp_path / "bgm").mkdir()
    monkeypatch.setenv("ZHIPU_API_KEY", "k1")
    monkeypatch.setenv("PEXELS_API_KEY", "k2")
    pc = P.run_precheck(cfg, tmp_path, probe_services=False)
    assert not pc.ok
    assert any("bgm" in r.name for r in pc.errors)


def test_missing_bgm_dir_blocks_startup(tmp_path, monkeypatch):
    cfg = base_cfg(tmp_path)                 # bgm 目录不存在
    monkeypatch.setenv("ZHIPU_API_KEY", "k1")
    monkeypatch.setenv("PEXELS_API_KEY", "k2")
    pc = P.run_precheck(cfg, tmp_path, probe_services=False)
    assert not pc.ok
    assert any("bgm" in r.name for r in pc.errors)


def test_missing_key_blocks_startup(tmp_path, monkeypatch):
    cfg = base_cfg(tmp_path)
    (tmp_path / "bgm").mkdir()
    (tmp_path / "bgm" / "a.mp3").write_bytes(b"x")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    pc = P.run_precheck(cfg, tmp_path, probe_services=False)
    assert not pc.ok
    assert any(r.name.startswith("env:") for r in pc.errors)
