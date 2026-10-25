# -*- coding: utf-8 -*-
"""启动前检查（P0-3）：在消耗任何 API 额度之前把问题拦下来。

检查层次（对应整改方案 P0-3 表格）：
  1. API 配置 —— 必需 Key 是否存在（只报告变量名，绝不输出值）
  2. 配置文件 —— YAML 可解析、必需字段齐全且类型正确
  3. 路径     —— BGM 曲库、工作目录可写、ffmpeg 可用
  4. 素材     —— 曲库里确实有可用 MP3
  5. 外部服务 —— 轻量连通性检查（区分网络失败与认证失败）
  6. 发布     —— 本项目为手动上传，无平台登录态；生成成片不需要任何平台登录

check_services=False 时跳过第 5 层（离线环境 / CI / 单元测试用）。
"""
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# YAML 里合法的 xfade 转场样式（与 ffmpeg 对齐的常用子集）
KNOWN_TRANSITIONS = {
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "circleopen", "circleclose", "fadeblack", "fadewhite", "slideleft",
    "slideright", "slideup", "slidedown", "smoothleft", "smoothright",
}

_PLACEHOLDER_PREFIXES = ("your_", "changeme", "xxx", "todo")


@dataclass
class CheckResult:
    name: str
    ok: bool
    level: str = "error"          # error | warning
    detail: str = ""


@dataclass
class Precheck:
    results: list = field(default_factory=list)

    def add(self, name: str, ok: bool, level: str = "error", detail: str = ""):
        self.results.append(CheckResult(name, ok, level, detail))

    @property
    def ok(self) -> bool:
        return not any((not r.ok) and r.level == "error" for r in self.results)

    @property
    def errors(self) -> list:
        return [r for r in self.results if not r.ok and r.level == "error"]

    @property
    def warnings(self) -> list:
        return [r for r in self.results if not r.ok and r.level == "warning"]

    def summary(self) -> str:
        lines = []
        for r in self.results:
            if r.ok:
                lines.append(f"  [ok]   {r.name}")
            else:
                mark = "ERROR" if r.level == "error" else "WARN "
                lines.append(f"  [{mark}] {r.name}: {r.detail}")
        return "\n".join(lines)

    def raise_if_failed(self):
        if not self.ok:
            failed = "; ".join(f"{r.name}: {r.detail}" for r in self.errors)
            raise RuntimeError(f"precheck failed — fix these before generating:\n"
                               f"{self.summary()}")


def mask(value: str) -> str:
    """密钥打码（未来如需在日志展示时使用）；绝不输出原文。"""
    v = (value or "").strip()
    return "****" if v else "(empty)"


# ── 1. 环境变量 ─────────────────────────────────────────
def check_env(required: tuple = ("ZHIPU_API_KEY", "PEXELS_API_KEY")) -> list:
    out = []
    for var in required:
        val = (os.environ.get(var) or "").strip()
        if not val:
            out.append(CheckResult(f"env:{var}", False, "error",
                                   "missing required API key"))
        elif any(val.lower().startswith(p) for p in _PLACEHOLDER_PREFIXES):
            out.append(CheckResult(f"env:{var}", False, "error",
                                   "still a placeholder value — put the real key in .env"))
        else:
            out.append(CheckResult(f"env:{var}", True, detail=f"set ({mask(val)})"))
    return out


# ── 2. 配置文件 ─────────────────────────────────────────
def config_problems(cfg: dict) -> list:
    """返回 (字段, 问题) 列表；空列表 = 配置合格。"""
    p = []
    topics = cfg.get("topics")
    if not isinstance(topics, list) or not topics:
        p.append(("topics", "must be a non-empty list"))
    vc = cfg.get("video") or {}
    for k in ("width", "height", "fps"):
        if not isinstance(vc.get(k), int) or vc[k] <= 0:
            p.append((f"video.{k}", "must be a positive integer"))
    lo, hi = vc.get("sections_min"), vc.get("sections_max")
    if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi):
        p.append(("video.sections_min/max", "need 1 <= sections_min <= sections_max"))
    cps = vc.get("chars_per_section")
    if not (isinstance(cps, (list, tuple)) and len(cps) == 2
            and 0 < cps[0] <= cps[1]):
        p.append(("video.chars_per_section", "must be [min_words, max_words]"))
    narr = cfg.get("narrator") or {}
    if narr.get("backend") not in ("edge", "zhipu"):
        p.append(("narrator.backend", "must be 'edge' or 'zhipu'"))
    if not str(narr.get("voice") or "").strip():
        p.append(("narrator.voice", "must not be empty"))
    cw = cfg.get("copywriter") or {}
    if not str(cw.get("model") or "").strip():
        p.append(("copywriter.model", "must not be empty"))
    temp = cw.get("temperature")
    if not isinstance(temp, (int, float)) or not 0 <= temp <= 2:
        p.append(("copywriter.temperature", "must be a number in [0, 2]"))
    vol = cfg.get("bgm_volume")
    if not isinstance(vol, (int, float)) or not 0 <= vol <= 1:
        p.append(("bgm_volume", "must be a number in [0, 1]"))
    tr = cfg.get("transition") or {}
    fade = tr.get("fade_seconds", 0.6)
    if not isinstance(fade, (int, float)) or not 0 <= fade <= 5:
        p.append(("transition.fade_seconds", "must be a number in [0, 5]"))
    styles = tr.get("styles")
    if styles is not None:
        if not isinstance(styles, list) or not styles:
            p.append(("transition.styles", "must be a non-empty list if present"))
        else:
            bad = [s for s in styles if s not in KNOWN_TRANSITIONS]
            if bad:
                p.append(("transition.styles",
                          f"unknown xfade styles: {bad}; known: {sorted(KNOWN_TRANSITIONS)}"))
    return p


def check_config(cfg: dict) -> list:
    out = []
    problems = config_problems(cfg)
    if problems:
        for field_, msg in problems:
            out.append(CheckResult(f"config:{field_}", False, "error", msg))
    else:
        out.append(CheckResult("config", True, detail="all required fields valid"))
    return out


# ── 3/4. 路径与素材 ─────────────────────────────────────
def check_paths(cfg: dict, project_dir: Path) -> list:
    out = []
    base = Path(project_dir)

    # BGM 曲库：必须存在且至少有 1 个 MP3（否则 random.choice 会在合成后才炸）
    bgm_dir = Path(str(cfg.get("bgm_dir") or "bgm"))
    if not bgm_dir.is_absolute():
        bgm_dir = base / bgm_dir
    if not bgm_dir.is_dir():
        out.append(CheckResult("bgm_dir", False, "error",
                               f"not found: {bgm_dir} (set bgm_dir in config.yaml)"))
    else:
        bgms = sorted(bgm_dir.glob("*.mp3"))
        if not bgms:
            out.append(CheckResult("bgm_dir", False, "error",
                                   f"no .mp3 files in {bgm_dir}"))
        else:
            out.append(CheckResult("bgm_dir", True, detail=f"{len(bgms)} mp3 files"))

    # ffmpeg 可用
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-version"], capture_output=True, text=True,
                           timeout=30)
        if r.returncode == 0:
            out.append(CheckResult("ffmpeg", True, detail=Path(exe).name))
        else:
            out.append(CheckResult("ffmpeg", False, "error", "ffmpeg -version failed"))
    except Exception as e:
        out.append(CheckResult("ffmpeg", False, "error",
                               f"unavailable: {type(e).__name__}: {e}"))

    # 工作目录可写（logs/data/output）
    for d in ("logs", "data", "output"):
        try:
            (base / d).mkdir(parents=True, exist_ok=True)
            probe = base / d / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            out.append(CheckResult(f"dir:{d}", True))
        except OSError as e:
            out.append(CheckResult(f"dir:{d}", False, "error",
                                   f"not writable: {e}"))

    # 本地 TTS 资产（缺失只降级到下一后端，不拦截启动）
    try:
        from . import narrator as _narr
        if not Path(_narr.PIPER_MODEL).is_file():
            out.append(CheckResult("tts:piper_model", False, "warning",
                                   f"piper model missing: {_narr.PIPER_MODEL}"))
        narr = cfg.get("narrator") or {}
        if (narr.get("cosy") or {}).get("enable") and not _narr.cosy_env_ready():
            out.append(CheckResult("tts:cosy_env", False, "warning",
                                   "cosy enabled but env missing — "
                                   "will fall back to edge/piper chain"))
    except Exception as e:  # narrator 导入失败不应阻断启动检查
        out.append(CheckResult("tts", False, "warning",
                               f"narrator import failed: {type(e).__name__}"))
    return out


# ── 5. 外部服务（轻量，最多 2 个请求） ──────────────────
def check_services(pexels_key: str, zhipu_key: str) -> list:
    import requests
    out = []
    # Pexels：一次真实搜索 —— 能同时区分 网络失败 / 认证失败 / 正常
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": pexels_key},
                         params={"query": "china", "per_page": 1}, timeout=15)
        if r.status_code == 200:
            out.append(CheckResult("svc:pexels", True))
        elif r.status_code in (401, 403):
            out.append(CheckResult("svc:pexels", False, "error",
                                   "auth failed (401/403) — check PEXELS_API_KEY"))
        else:
            out.append(CheckResult("svc:pexels", False, "warning",
                                   f"unexpected status {r.status_code}"))
    except requests.RequestException as e:
        out.append(CheckResult("svc:pexels", False, "error",
                               f"network unreachable: {type(e).__name__}"))

    # 智谱：只测连通性（不发真实对话请求、不消耗额度；认证在首次调用时验证）
    try:
        r = requests.get("https://open.bigmodel.cn/api/paas/v4", timeout=15)
        if r.status_code < 500:
            out.append(CheckResult("svc:zhipu", True,
                                   detail=f"reachable (http {r.status_code}); "
                                          f"auth verified on first call"))
        else:
            out.append(CheckResult("svc:zhipu", False, "warning",
                                   f"server error http {r.status_code}"))
    except requests.RequestException as e:
        out.append(CheckResult("svc:zhipu", False, "error",
                               f"network unreachable: {type(e).__name__}"))
    return out


def run_precheck(cfg: dict, project_dir, pexels_key: str = "", zhipu_key: str = "",
                 probe_services: bool = True) -> Precheck:
    """完整启动前检查。pex/zhipu key 只用于服务连通性检查，不会被打印。"""
    pc = Precheck()
    for r in check_env():
        pc.results.append(r)
    for r in check_config(cfg):
        pc.results.append(r)
    for r in check_paths(cfg, Path(project_dir)):
        pc.results.append(r)
    # Key 缺失时不再发外网请求（env 检查已经报错，连通性无意义）
    if probe_services and pexels_key and zhipu_key:
        for r in check_services(pexels_key, zhipu_key):
            pc.results.append(r)
    return pc
