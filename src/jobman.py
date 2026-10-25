# -*- coding: utf-8 -*-
"""任务目录 + manifest（P1-1 断点续跑核心）。

每期视频一个独立任务目录：
    data/jobs/<job_id>/
        manifest.json      —— 阶段状态 / 输入输出 / 失败原因 / 重试次数 / 耗时
        script.json        —— 讲稿（含每段 video_query）
        narration/sec_XX.mp3
        materials/raw_XX.mp4 + index.json（素材来源清单）
        clips/sec_XX.mp4
        final.mp4 / cover.jpg / final.srt / upload_info.txt
        costs.jsonl / logs/events.jsonl

断点续跑规则（重试粒度到"段"）：
- 阶段 done 的直接跳过；per-section 依据内容指纹（cachekeys）判断可否复用。
- 输入发生实质变化（config_hash / script_hash / 段落指纹）→ 对应阶段重做。
- manifest 保存全部状态，进程崩溃后重跑即自动从失败阶段继续。
"""
import datetime
import json
import os
import re
from pathlib import Path

STAGES = ("script", "narration", "materials", "clips", "compose", "qc",
          "review", "package")


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len] or "job"


class Manifest:
    """manifest.json 的读写封装。save() 原子落盘（tmp + os.replace）。"""

    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path

    # ── 阶段 ─────────────────────────────────────────────
    def stage(self, name: str) -> dict:
        return self.data.setdefault("stages", {}).setdefault(
            name, {"status": "pending", "attempts": 0})

    def stage_start(self, name: str) -> dict:
        st = self.stage(name)
        st.update(status="running", attempts=st.get("attempts", 0) + 1,
                  started_at=_now(), error=None)
        self.save()
        return st

    def stage_done(self, name: str, duration_s: float = None, **extra):
        st = self.stage(name)
        st.update(status="done", finished_at=_now(), error=None)
        if duration_s is not None:
            st["duration_s"] = round(duration_s, 2)
        st.update(extra)
        self.save()

    def stage_fail(self, name: str, error: str, retryable: bool = True):
        self.stage(name).update(status="failed", error=str(error)[:2000],
                                retryable=retryable, finished_at=_now())
        self.save()

    def stage_status(self, name: str) -> str:
        return self.stage(name).get("status", "pending")

    def done_stages(self) -> set:
        return {n for n, st in self.data.get("stages", {}).items()
                if st.get("status") == "done"}

    def first_unfinished(self) -> str | None:
        for name in STAGES:
            if self.stage_status(name) != "done":
                return name
        return None

    # ── 段级（断点到段） ─────────────────────────────────
    def section(self, i: int) -> dict:
        return self.data.setdefault("sections", {}).setdefault(str(i), {})

    def set_section(self, i: int, kind: str, payload: dict):
        self.section(i)[kind] = payload
        self.save()

    # ── 其他 ─────────────────────────────────────────────
    def set(self, key: str, value):
        self.data[key] = value
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)


class Job:
    """一个任务目录的句柄：路径约定 + manifest。"""

    def __init__(self, dir_path: Path, manifest: Manifest):
        self.dir = Path(dir_path)
        self.manifest = manifest

    # 路径
    @property
    def script_json(self): return self.dir / "script.json"

    @property
    def narration_dir(self): return self.dir / "narration"

    @property
    def materials_dir(self): return self.dir / "materials"

    @property
    def materials_index(self): return self.dir / "materials" / "index.json"

    @property
    def clips_dir(self): return self.dir / "clips"

    @property
    def final(self): return self.dir / "final.mp4"

    @property
    def cover(self): return self.dir / "cover.jpg"

    @property
    def srt(self): return self.dir / "final.srt"

    @property
    def upload_info(self): return self.dir / "upload_info.txt"

    @property
    def logs_dir(self): return self.dir / "logs"

    def narration_path(self, i: int):
        # narrator 允许降级为 wav（SAPI 兜底），两种都可能存在
        return self.narration_dir / f"sec_{i:02d}.mp3"

    def raw_path(self, i: int): return self.materials_dir / f"raw_{i:02d}.mp4"

    def clip_path(self, i: int): return self.clips_dir / f"sec_{i:02d}.mp4"

    def ensure_dirs(self):
        for d in (self.narration_dir, self.materials_dir, self.clips_dir,
                  self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_job(jobs_root, topic: str, cfg: dict, config_hash: str,
            prompt_version: str = "") -> Job:
    """创建新任务目录 + 初始 manifest。"""
    base = Path(jobs_root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    job_id = f"{stamp}-{slugify(topic, 40)}"
    jd = base / job_id
    n = 2
    while jd.exists():
        jd = base / f"{job_id}-{n}"
        n += 1
    jd.mkdir(parents=True)
    mf = Manifest({
        "job_id": jd.name, "topic": topic, "created_at": _now(),
        "config_hash": config_hash, "prompt_version": prompt_version,
        "stages": {}, "sections": {}, "outputs": {},
    }, jd / "manifest.json")
    job = Job(jd, mf)
    job.ensure_dirs()
    mf.save()
    return job


def load_job(jobs_root, job_id: str) -> Job | None:
    jd = Path(jobs_root) / job_id
    mf_path = jd / "manifest.json"
    if not mf_path.is_file():
        return None
    return Job(jd, Manifest(json.loads(mf_path.read_text(encoding="utf-8")),
                            mf_path))


def all_jobs(jobs_root) -> list:
    root = Path(jobs_root)
    if not root.is_dir():
        return []
    out = []
    for mf in sorted(root.glob("*/manifest.json")):
        try:
            out.append(Job(mf.parent, Manifest(
                json.loads(mf.read_text(encoding="utf-8")), mf)))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def find_resumable(jobs_root, topic: str, config_hash: str) -> Job | None:
    """同选题 + 同配置指纹、且尚未打包完成的任务 → 断点续跑对象。

    打包完成（package done）的任务不续跑 —— 选题状态机已把它标 ready。
    """
    for job in all_jobs(jobs_root):
        mf = job.manifest.data
        if (mf.get("topic") == topic and mf.get("config_hash") == config_hash
                and mf.get("stages", {}).get("package", {}).get("status") != "done"):
            return job
    return None


def log_event(job: Job, event: str, **fields):
    """结构化事件日志（logs/events.jsonl）：失败原因、缓存命中、用量等都走这里。"""
    entry = {"ts": _now(), "event": event, **fields}
    job.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(job.logs_dir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
