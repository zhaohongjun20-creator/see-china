# -*- coding: utf-8 -*-
"""成本账单（P2-1）：按任务、阶段、服务记录真实消耗，落盘 costs.jsonl。

原则（对应整改方案 P2-1）：
- 服务端返回 usage 时记 real_tokens（可信）；没有时只记字符数，并明确标注
  tokens_estimated=True —— 绝不把字符数冒充成"实际 Token"。
- 每条记录带 service / stage / ts，summary() 按 service 聚合。
"""
import datetime
import json
from pathlib import Path


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, service: str, stage: str = "", **fields):
        entry = {"ts": _ts(), "service": service, "stage": stage, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def record_llm(self, stage: str, usage: dict, model: str = ""):
        """usage 来自 script._Usage.as_dict() 形状；无真实 token 时标注估算。"""
        self.record(
            "llm", stage, model=model, calls=int(usage.get("calls", 0)),
            real_tokens=int(usage.get("real_tokens", 0)),
            tokens_estimated=bool(usage.get("tokens_estimated")),
            chars_in=int(usage.get("chars_in", 0)),
            chars_out=int(usage.get("chars_out", 0)))

    def record_ffmpeg(self, stage: str, seconds: float, ok: bool = True):
        self.record("ffmpeg", stage, seconds=round(seconds, 2), ok=ok)

    def lines(self) -> list:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def summary(self) -> dict:
        """按 service 聚合：{service: {count, ...合计字段}}。"""
        agg = {}
        for e in self.lines():
            svc = agg.setdefault(e["service"], {"count": 0})
            svc["count"] += 1
            for k, v in e.items():
                if k in ("ts", "service", "stage"):
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if k.endswith(("_s", "_seconds", "seconds")):
                        key = "total_seconds"
                    else:
                        key = f"total_{k}" if not k.startswith("total_") else k
                    svc[key] = svc.get(key, 0) + v
                elif k in ("tokens_estimated", "ok") and v:
                    svc[f"any_{k}"] = True
        return agg


class Timer:
    """阶段耗时上下文管理器：with Timer() as t: ... t.seconds"""

    def __init__(self):
        self.seconds = 0.0
        self._t0 = None

    def __enter__(self):
        import time
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        import time
        self.seconds = time.monotonic() - self._t0
        return False
