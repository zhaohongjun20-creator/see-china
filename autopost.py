# -*- coding: utf-8 -*-
"""《带你看中国 See China》——英文中国主题视频产线（阶段化 + 断点续跑版）。

管线（每阶段落盘 manifest，可从任意失败点恢复）：
    precheck 启动检查（不耗额度）→ 单实例锁 → 原子领取选题
    → script 讲稿 → narration 逐段配音 → materials 逐段搜素材(全局缓存)
    → clips 逐段渲染 → compose 合成(音画对齐) → qc 技术质检
    → review 内容审核(异常不放行) → package 上传素材包

CLI：
    python autopost.py                     # 产出一期（自动跳过已完成选题，断点续跑）
    python autopost.py 3                   # 指定主题序号
    python autopost.py run --no-services   # 跳过外网连通性检查（离线/CI）
    python autopost.py status              # 选题与任务状态总览
    python autopost.py redo 3              # 重置某选题（清零失败计数）重新制作
    python autopost.py review-job <id> --approve|--reject   # 人工审核裁决
    python autopost.py mark-published 3    # 人工上传成功后回写状态

产出：data/jobs/<job_id>/final.mp4 + cover.jpg + final.srt + upload_info.txt
"""
import argparse
import json
import logging
import os
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from src import cachekeys
from src import costs
from src import jobman
from src import locks
from src import material
from src import narrator
from src import precheck
from src import qc
from src import review as review_mod
from src import script as script_mod
from src import section_clip
from src import slideshow
from src.store import GENERATING, Store

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
JOBS_DIR = DATA_DIR / "jobs"
LOG_DIR = BASE / "logs"
SERIES = "See China"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / f"{datetime.now():%Y-%m}.log",
                                      encoding="utf-8"),
                  logging.StreamHandler()])


def load_config() -> dict:
    import yaml
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


class HumanReviewRequired(Exception):
    """审核发现 high 级问题，等待人工裁决（CLI review-job）。"""


def _transition_params(cfg: dict) -> tuple:
    tr = cfg.get("transition") or {}
    fade = float(tr.get("fade_seconds", 0.6))
    tail = float(tr.get("tail_seconds", fade))
    end_hold = float(tr.get("end_hold_seconds", 2.0))
    return fade, tail, end_hold


def section_need(dur: float, index: int, total: int, cfg: dict) -> float:
    """段画面需要多长：旁白 + 溶解 fade + 停留 tail（末段为片尾定格 end_hold）。"""
    fade, tail, end_hold = _transition_params(cfg)
    return dur + fade + (tail if index < total - 1 else end_hold)


def _extract_cover(final, cover):
    import imageio_ffmpeg
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", "2",
                    "-i", str(final), "-frames:v", "1", str(cover)],
                   capture_output=True, timeout=120)


# ── 主流程 ──────────────────────────────────────────────
def run(topic_index: int | None = None, redo: bool = False,
        check_services: bool = True) -> int:
    log = logging.getLogger("main")
    load_dotenv(BASE / ".env")
    cfg = load_config()
    zhipu = os.environ.get("ZHIPU_API_KEY", "")
    pex = os.environ.get("PEXELS_API_KEY", "")

    # ① 启动前检查：在消耗任何 API 额度之前把配置/路径/素材/服务问题拦下
    pc = precheck.run_precheck(cfg, BASE, pex, zhipu,
                               check_services=check_services)
    log.info("Precheck:\n%s", pc.summary())
    if pc.warnings:
        log.warning("precheck warnings: %d", len(pc.warnings))
    pc.raise_if_failed()

    store = Store(str(DATA_DIR / "topics.db"))
    topics = cfg["topics"]

    # ② 单实例锁（原子创建，崩溃残留自动接管）
    with locks.InstanceLock(DATA_DIR / "autopost.lock"):
        # ③ 选题（默认跳过已完成；失败未超上限的可重试）
        if topic_index is not None:
            if not 1 <= topic_index <= len(topics):
                log.error("topic index %d out of range 1..%d",
                          topic_index, len(topics))
                return 1
            topic = topics[topic_index - 1]
            if redo:
                store.reset_topic(topic)
                log.info("topic reset to pending: %s", topic)
        else:
            cands = store.pending_topics(topics, cfg.get("max_retries", 3))
            if not cands:
                log.warning("主题池已全部做完（或失败重试耗尽），请在 config.yaml 补充；"
                            "redo <序号> 可重置失败选题")
                return 0
            topic = random.choice(cands)

        if store.is_topic_done(topic) and not redo:
            log.info("topic already done, skip: %s", topic)
            return 0

        # ④ 原子领取：并发时同一选题只会被一个进程拿到
        if not store.claim_topic(topic, cfg.get("max_retries", 3)):
            row = store.topic_row(topic)
            if row and row["status"] == GENERATING:
                store.reclaim_stuck(topic)   # 崩溃残留；已持锁，安全
                log.info("reclaimed stuck topic: %s", topic)
            else:
                log.info("topic not claimable (done/exhausted/in-flight): %s",
                         topic)
                return 0
        log.info("Episode topic: %s", topic)

        try:
            return _produce(topic, cfg, store, zhipu, pex)
        except HumanReviewRequired:
            return 2
        except Exception as e:
            log.error("production failed: %s", e)
            return 1


def _produce(topic: str, cfg: dict, store: Store, zhipu: str, pex: str) -> int:
    log = logging.getLogger("produce")
    config_hash = cachekeys.config_fingerprint(cfg, script_mod.PROMPT_VERSION)

    # 断点续跑：同选题 + 同配置指纹、未打包完成的任务直接接着做
    job = jobman.find_resumable(JOBS_DIR, topic, config_hash)
    if job:
        log.info("resuming job %s (first unfinished: %s)", job.dir.name,
                 job.manifest.first_unfinished())
    else:
        job = jobman.new_job(JOBS_DIR, topic, cfg, config_hash,
                             prompt_version=script_mod.PROMPT_VERSION)
        log.info("new job %s", job.dir.name)
    mf = job.manifest
    ledger = costs.Ledger(job.dir / "costs.jsonl")
    jobman.log_event(job, "job_started", topic=topic, resumed=bool(job))
    state = {"stage": None, "topic": topic, "doc": None}

    def fail(stage: str, err: Exception):
        mf.stage_fail(stage, str(err))
        store.mark_failed(topic, stage, str(err))
        jobman.log_event(job, "stage_failed", stage=stage, error=str(err)[:500])

    def stage(name: str, skip_condition, fn) -> bool:
        """阶段执行器：done 则跳过（断点续跑）；异常则落状态再上抛。

        HumanReviewRequired 特殊处理：审核待人工裁决不算失败 —— 阶段标记
        完成，选题状态已是 review（不可自动领取），等 CLI 裁决。
        """
        if skip_condition():
            log.info("[%s] already done — resume skip", name)
            return False
        mf.stage_start(name)
        state["stage"] = name
        import time
        t0 = time.monotonic()
        try:
            fn()
        except HumanReviewRequired:
            mf.stage_done(name, duration_s=time.monotonic() - t0)
            raise
        except Exception as e:
            fail(name, e)
            raise
        mf.stage_done(name, duration_s=time.monotonic() - t0)
        state["stage"] = None
        return True

    # ── 阶段 1：讲稿 ─────────────────────────────────────
    def script_done():
        return (mf.stage_status("script") == "done" and job.script_json.is_file())

    def do_script():
        doc = script_mod.generate(
            zhipu, topic, model=cfg["copywriter"]["model"],
            temperature=cfg["copywriter"]["temperature"],
            n_min=cfg["video"]["sections_min"], n_max=cfg["video"]["sections_max"],
            c_lo=cfg["video"]["chars_per_section"][0],
            c_hi=cfg["video"]["chars_per_section"][1],
            max_fix_attempts=cfg.get("script", {}).get("max_fix_attempts", 2))
        job.script_json.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        mf.set("script_hash", cachekeys.script_fingerprint(doc))
        ledger.record_llm("script", doc["_usage"], cfg["copywriter"]["model"])
        jobman.log_event(job, "script_done", title=doc["title"],
                         sections=len(doc["sections"]),
                         warnings=doc.get("_warnings", []))

    stage("script", script_done, do_script)
    doc = json.loads(job.script_json.read_text(encoding="utf-8"))
    state["doc"] = doc
    script_hash = mf.data.get("script_hash") or cachekeys.script_fingerprint(doc)
    log.info("Script: %s | %d sections", doc["title"], len(doc["sections"]))
    for w in doc.get("_warnings", []):
        log.warning("script warning: %s", w)

    # ── 阶段 2：逐段配音（段级缓存：文本/音色/后端未变则复用） ──
    narr_cfg = cfg["narrator"]
    backend = narr_cfg.get("backend", "edge")

    def narration_done():
        return mf.stage_status("narration") == "done"

    def do_narration():
        job.narration_dir.mkdir(parents=True, exist_ok=True)
        z = {"api_key": zhipu, **(narr_cfg.get("zhipu") or {})} \
            if backend == "zhipu" else None
        reused = total_s = 0
        for i, sec in enumerate(doc["sections"]):
            fp = cachekeys.narration_fingerprint(
                sec["text"], narr_cfg["voice"], narr_cfg.get("rate", "+0%"),
                backend, script_hash)
            rec = mf.section(i).get("narration")
            if (rec and rec.get("hash") == fp and rec.get("duration", 0) > 0
                    and Path(rec["file"]).is_file()):
                reused += 1
                total_s += rec["duration"]
                jobman.log_event(job, "cache_hit", stage="narration", section=i)
                continue
            for old in (job.narration_path(i),
                        job.narration_path(i).with_suffix(".wav")):
                Path(old).unlink(missing_ok=True)
            actual, used_backend, used_voice = narrator.synth_section(
                sec["text"], str(job.narration_path(i)), narr_cfg["voice"],
                narr_cfg.get("rate", "+0%"), backend=backend, zhipu=z,
                allow_sapi=bool(narr_cfg.get("allow_sapi_fallback", False)))
            dur = narrator.audio_duration(actual)
            if dur <= 0:
                raise RuntimeError(f"narration {i}: zero-length audio")
            mf.set_section(i, "narration",
                           {"hash": fp, "file": str(actual), "duration": dur,
                            "backend": backend, "backend_used": used_backend,
                            "voice": used_voice})
            if used_backend == "sapi":
                mf.set("sapi_fallback_used", True)   # 机械音 → 强制人工审核
                jobman.log_event(job, "sapi_fallback_used", section=i)
            total_s += dur
            jobman.log_event(job, "narration_section", section=i, duration=dur,
                             file=Path(actual).name, backend_used=used_backend,
                             voice=used_voice)
        ledger.record("tts", "narration", backend=backend,
                      sections=len(doc["sections"]), reused=reused,
                      audio_seconds=round(total_s, 1))
        log.info("Narration: %d sections (%.1fs), %d reused",
                 len(doc["sections"]), total_s, reused)

    stage("narration", narration_done, do_narration)

    # ── 阶段 3：逐段素材（改写阶梯 + 全局缓存 + 来源清单） ──
    fade, tail, end_hold = _transition_params(cfg)
    nsec = len(doc["sections"])
    cache_dir = DATA_DIR / "material_cache"

    def materials_done():
        return mf.stage_status("materials") == "done"

    def do_materials():
        records = [None] * nsec
        used, recent = set(), material.load_recent_ids(
            DATA_DIR / "recent_assets.json")
        searches = downloads = hits = 0
        for i, sec in enumerate(doc["sections"]):
            vrec = mf.section(i)["narration"]
            need = section_need(vrec["duration"], i, nsec, cfg)
            fp = cachekeys.stable_hash({"q": sec["video_query"],
                                        "need": round(need, 2)})
            rec = mf.section(i).get("material")
            if (rec and rec.get("hash") == fp and rec.get("asset_id")
                    and Path(rec["file"]).is_file()):
                used.add(rec["asset_id"])
                records[i] = rec
                hits += 1
                continue
            got = material.search_footage(pex, sec["video_query"], need, used,
                                          topic=topic, recent_ids=recent)
            searches += 1
            if got is None:
                raise RuntimeError(
                    f"section {i}: no usable footage for "
                    f"'{sec['video_query']}' (rewrite ladder exhausted) — "
                    f"请人工指定素材或修改取材词后重跑")
            path, hit = material.download_cached(got["link"], cache_dir,
                                                 got["asset_id"])
            downloads += 0 if hit else 1
            if got["fallback"]:
                log.warning("section %d: FALLBACK footage '%s' for query '%s'",
                            i, got["query_used"], sec["video_query"])
            rec = {**got, "hash": fp, "file": path, "section": i}
            mf.set_section(i, "material", rec)
            used.add(got["asset_id"])
            records[i] = rec
            jobman.log_event(job, "material_section", section=i,
                             query_used=got["query_used"],
                             ladder_step=got["ladder_step"],
                             asset_id=got["asset_id"], cache_hit=hit)
        # 素材来源清单（追溯：asset_id / source_url / license / 阶梯层级）
        job.materials_index.write_text(
            json.dumps([r for r in records if r], ensure_ascii=False, indent=1),
            encoding="utf-8")
        material.remember_ids(DATA_DIR / "recent_assets.json", list(used))
        ledger.record("materials", "materials", searches=searches,
                      downloads=downloads, cache_hits=hits)
        log.info("Materials: %d searched, %d downloaded, %d cache hits",
                 searches, downloads, hits)

    stage("materials", materials_done, do_materials)
    records = [mf.section(i)["material"] for i in range(nsec)]

    # ── 阶段 4：逐段渲染（片段级缓存） ────────────────────
    vc = cfg["video"]

    def clips_done():
        return mf.stage_status("clips") == "done"

    def do_clips():
        job.clips_dir.mkdir(parents=True, exist_ok=True)
        for i in range(nsec):
            vrec, mrec = mf.section(i)["narration"], mf.section(i)["material"]
            need = section_need(vrec["duration"], i, nsec, cfg)
            fp = cachekeys.clip_fingerprint(need, vc["width"], vc["height"],
                                            vc["fps"],
                                            cachekeys.file_hash(mrec["file"]))
            crec = mf.section(i).get("clip")
            clip = job.clip_path(i)
            if crec and crec.get("hash") == fp and clip.is_file():
                continue
            clip.unlink(missing_ok=True)
            res = section_clip.build(mrec["file"], need, str(clip))
            mf.set_section(i, "clip", {"hash": fp, "file": str(clip),
                                       "need": round(need, 3)})
            # 片段使用区间写回素材记录（来源追溯：用了素材的哪一段）
            mf.set_section(i, "material",
                           {**mrec, "segment_start": res["start"],
                            "segment_seconds": round(need, 3),
                            "loop_fill": res["loop"]})
            jobman.log_event(job, "clip_rendered", section=i,
                             segment_start=res["start"], loop=res["loop"])
        # 素材清单补上片段区间后重写
        final_records = [mf.section(i)["material"] for i in range(nsec)]
        job.materials_index.write_text(
            json.dumps(final_records, ensure_ascii=False, indent=1),
            encoding="utf-8")

    stage("clips", clips_done, do_clips)

    # ── 阶段 5：合成（音画对齐；参数未变则复用成片） ────────
    bgm_dir = Path(cfg.get("bgm_dir") or "bgm")
    if not bgm_dir.is_absolute():
        bgm_dir = (BASE / bgm_dir).resolve()
    bgms = sorted(bgm_dir.glob("*.mp3"))

    def compose_done():
        return (mf.stage_status("compose") == "done" and job.final.is_file()
                and job.srt.is_file())

    def do_compose():
        # BGM 选择也纳入缓存键：重跑同一任务沿用同一首曲子
        st = mf.stage("compose")
        bgm_name = st.get("bgm") if st.get("bgm") in \
            [b.name for b in bgms] else None
        if bgm_name is None:
            bgm_name = random.choice(bgms).name
            st["bgm"] = bgm_name
            mf.save()
        srt_hash = cachekeys.stable_hash(
            [{"t": s["text"],
              "d": mf.section(i)["narration"]["duration"]}
             for i, s in enumerate(doc["sections"])])
        fp = cachekeys.compose_fingerprint(
            [mf.section(i)["clip"]["hash"] for i in range(nsec)],
            bgm_name, cfg["bgm_volume"], fade,
            cfg.get("transition", {}).get("styles"), srt_hash)
        if st.get("hash") == fp and job.final.is_file():
            return
        job.final.unlink(missing_ok=True)   # 上次 qc 失败的坏片不允许混用
        import time
        t0 = time.monotonic()
        res = slideshow.compose_final(
            [mf.section(i)["clip"]["file"] for i in range(nsec)],
            [mf.section(i)["narration"]["file"] for i in range(nsec)],
            doc["sections"], str(job.final), str(bgm_dir / bgm_name),
            str(job.srt), bgm_volume=float(cfg["bgm_volume"]), fade=fade,
            transitions=cfg.get("transition", {}).get("styles"))
        ledger.record_ffmpeg("compose", time.monotonic() - t0)
        st.update({"hash": fp, "duration": res["duration"]})
        mf.save()
        jobman.log_event(job, "compose_done", duration=res["duration"],
                         bgm=bgm_name)

    stage("compose", compose_done, do_compose)
    _extract_cover(job.final, job.cover)

    # ── 阶段 6：技术质检（不合格拦截，不进发布包） ─────────
    def qc_done():
        return mf.stage_status("qc") == "done"

    def do_qc():
        expected = (sum(mf.section(i)["narration"]["duration"]
                        for i in range(nsec))
                    + (fade + tail) * (nsec - 1) + end_hold)
        ok, results = qc.run_qc(
            job.final, job.srt, job.cover, expected_duration=expected,
            spec=(vc["width"], vc["height"], vc["fps"]),
            thresholds=cfg.get("qc"))
        mf.set("qc_results", results)
        jobman.log_event(job, "qc", ok=ok,
                         errors=[r["detail"] for r in results
                                 if not r["ok"] and r["severity"] == "error"])
        if not ok:
            errors = "; ".join(r["detail"] for r in results
                               if not r["ok"] and r["severity"] == "error")
            raise RuntimeError(f"QC failed: {errors}")

    stage("qc", qc_done, do_qc)

    # ── 阶段 7：内容审核（异常不放行；high 级问题等人工裁决） ──
    def review_done():
        st = mf.stage("review")
        return st.get("status") == "done" and st.get("decision") in (
            "ready", "approved")

    def do_review():
        rev_cfg = cfg.get("review") or {}
        result = review_mod.run_review(zhipu, doc,
                                       model=rev_cfg.get("model",
                                                         cfg["copywriter"]["model"]))
        decision = review_mod.decide_with_flags(
            result, sapi_used=bool(mf.data.get("sapi_fallback_used")))
        mf.set("review", {"status": result["status"], "decision": decision,
                          "issues": result["issues"],
                          "deterministic_issues": result["deterministic_issues"]})
        ledger.record("review", "review", model=rev_cfg.get(
            "model", cfg["copywriter"]["model"]), status=result["status"],
            issues=len(result["issues"]))
        jobman.log_event(job, "review", decision=decision,
                         issues=result["issues"][:10])
        if decision == "retry":
            raise RuntimeError(
                "review channel unavailable — files kept, will retry "
                "next run（审核通道不可用，不得自动放行）")
        if decision == "human":
            store.mark_review(topic, doc["title"])
            raise HumanReviewRequired(
                f"review flagged → awaiting human decision. "
                f"裁决：python autopost.py review-job {job.dir.name} --approve|--reject")

    stage("review", review_done, do_review)
    # 之前已裁决过 human（stage done 但 decision=human）→ 再次提示，不进打包
    if mf.stage("review").get("decision") == "human":
        raise HumanReviewRequired(
            f"review still awaiting human decision. "
            f"裁决：python autopost.py review-job {job.dir.name} --approve|--reject")

    # ── 阶段 8：上传素材包（upload_info 供人工上传海外平台） ──
    def package_done():
        st = mf.stage("package")
        return st.get("status") == "done" and job.upload_info.is_file()

    def do_package():
        if not job.cover.is_file():
            _extract_cover(job.final, job.cover)
        records = [mf.section(i)["material"] for i in range(nsec)]
        credits = []
        for r in records:
            author = f" by {r['author']}" if r.get("author") else ""
            seg = (f", used {r.get('segment_start', 0):.1f}s-"
                   f"{r.get('segment_start', 0) + r.get('segment_seconds', 0):.1f}s"
                   if r.get("segment_seconds") else "")
            credits.append(f"Pexels #{r['asset_id']}{author}: "
                           f"'{r['query_used']}'{seg} ({r['source_url']}) — "
                           f"{r['license']}")
        tags = doc.get("tags", [])[:8] + cfg.get("hashtags", [])
        info = (
            f"TITLE: {doc['title']}\n\n"
            f"DESCRIPTION:\n{doc.get('description', '')}\n\n"
            f"A journey through real China — history, food, technology and daily "
            f"life. New episodes weekly. Subscribe and see China with your own eyes.\n\n"
            f"Footage credits:\n" + "\n".join(dict.fromkeys(credits)) +
            f"\n\nTAGS: {', '.join(dict.fromkeys(tags))}\n"
            f"IG_CAPTION: {doc['title']} 🇨🇳 #seechina #china #travelchina "
            f"#chineseculture #discoverchina #travel\n"
        )
        job.upload_info.write_text(info, encoding="utf-8")
        mf.set("outputs", {"final": str(job.final), "cover": str(job.cover),
                           "srt": str(job.srt),
                           "upload_info": str(job.upload_info),
                           "materials_index": str(job.materials_index)})
        store.mark_ready(topic, doc["title"])
        jobman.log_event(job, "episode_done")

    stage("package", package_done, do_package)

    # 汇总
    size_mb = job.final.stat().st_size / 1e6
    summary = ledger.summary()
    log.info("📦 %s | %.1f MB | costs: %s", job.final, size_mb,
             {k: v.get("count") for k, v in summary.items()})
    return 0


# ── 子命令 ──────────────────────────────────────────────
def cmd_status():
    store = Store(str(DATA_DIR / "topics.db"))
    print("== topics ==")
    for r in store.all_topic_rows():
        extra = f" fail={r['fail_stage']}: {r['fail_reason'][:80]}" \
            if r["status"] == "failed" else ""
        print(f"  [{r['status']:>10}] {r['topic'][:52]:<52} "
              f"retries={r['retries']}{extra}")
    print("== jobs ==")
    for j in jobman.all_jobs(JOBS_DIR):
        mf = j.manifest.data
        stages = " ".join(
            f"{s}:{mf.get('stages', {}).get(s, {}).get('status', '-')}"
            for s in jobman.STAGES)
        print(f"  {j.dir.name}  {stages}")
    return 0


def cmd_review_job(job_id: str, approve: bool) -> int:
    job = jobman.load_job(JOBS_DIR, job_id)
    if not job:
        print(f"job not found: {job_id}")
        return 1
    mf = job.manifest
    rev = mf.data.get("review") or {}
    print("Review issues:")
    for it in rev.get("issues", []):
        print(f"  [{it.get('severity')}] section {it.get('section')}: "
              f"{it.get('issue')} (source needed: {it.get('needs_source')})")
        if it.get("suggestion"):
            print(f"      suggestion: {it['suggestion']}")
    store = Store(str(DATA_DIR / "topics.db"))
    topic = mf.data["topic"]
    if not approve:
        store.mark_failed(topic, "review", "rejected by human review")
        mf.stage_fail("review", "rejected by human review", retryable=False)
        print("REJECTED — topic marked failed.")
        return 0
    # 批准：补完 package 阶段（成片与封面已在任务目录中）
    cfg = load_config()
    doc = json.loads(job.script_json.read_text(encoding="utf-8"))
    rev["decision"] = "approved"
    mf.stage("review").update({"status": "done", "decision": "approved"})
    mf.save()
    records = [mf.section(i)["material"] for i in range(len(doc["sections"]))]
    credits = []
    for r in records:
        author = f" by {r['author']}" if r.get("author") else ""
        credits.append(f"Pexels #{r['asset_id']}{author}: '{r['query_used']}' "
                       f"({r['source_url']}) — {r['license']}")
    tags = doc.get("tags", [])[:8] + cfg.get("hashtags", [])
    info = (f"TITLE: {doc['title']}\n\nDESCRIPTION:\n{doc.get('description','')}\n\n"
            f"Footage credits:\n" + "\n".join(dict.fromkeys(credits)) +
            f"\n\nTAGS: {', '.join(dict.fromkeys(tags))}\n")
    job.upload_info.write_text(info, encoding="utf-8")
    mf.set("outputs", {"final": str(job.final), "cover": str(job.cover),
                       "srt": str(job.srt), "upload_info": str(job.upload_info),
                       "materials_index": str(job.materials_index)})
    mf.stage_done("package")
    store.mark_ready(topic, doc["title"])
    print("APPROVED — topic marked ready; upload_info.txt generated.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="See China episode producer")
    ap.add_argument("topic", nargs="?", type=int,
                    help="主题序号(从1)；等价于 run <topic>")
    sub = ap.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run", help="produce one episode")
    run_p.add_argument("topic", nargs="?", type=int, help="主题序号(从1)")
    run_p.add_argument("--redo", action="store_true",
                       help="指定序号时先重置该选题")
    run_p.add_argument("--no-services", action="store_true",
                       help="跳过外网连通性检查")
    sub.add_parser("status", help="show topics & jobs")
    redo_p = sub.add_parser("redo", help="reset topic & retry")
    redo_p.add_argument("topic", type=int)
    rj = sub.add_parser("review-job", help="human review decision")
    rj.add_argument("job_id")
    g = rj.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    mp = sub.add_parser("mark-published", help="manual upload done")
    mp.add_argument("topic", type=int)
    args = ap.parse_args()

    setup_logging()
    log = logging.getLogger("cli")
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "redo":
        store = Store(str(DATA_DIR / "topics.db"))
        cfg = load_config()
        topic = cfg["topics"][args.topic - 1]
        store.reset_topic(topic)
        log.info("reset: %s", topic)
        return 0
    if args.cmd == "review-job":
        return cmd_review_job(args.job_id, args.approve)
    if args.cmd == "mark-published":
        store = Store(str(DATA_DIR / "topics.db"))
        cfg = load_config()
        topic = cfg["topics"][args.topic - 1]
        store.mark_published(topic)
        log.info("published: %s", topic)
        return 0
    # 默认 run
    idx = getattr(args, "topic", None)
    try:
        return run(idx, redo=getattr(args, "redo", False),
                   check_services=not getattr(args, "no_services", False))
    except RuntimeError as e:
        # precheck 失败 / 锁被占用等可预期错误：干净报错，不留 traceback
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
