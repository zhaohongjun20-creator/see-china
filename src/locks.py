# -*- coding: utf-8 -*-
"""单实例原子锁（P1-2）。

修复旧实现"检查文件存在 → 再写入"的两步竞态：这里创建与独占检查合并为
一次原子系统调用（os.open 的 O_CREAT|O_EXCL，Windows/POSIX 均原子），
两个进程同时启动时只有一个能成功创建锁文件。

锁文件内容为 JSON（pid/host/started），便于诊断与残留恢复：
- 持有进程已死亡（崩溃残留）→ 自动接管
- 锁龄超过 max_age_seconds → 视为过期残留，自动接管并告警
- 释放永远走 release()/finally，不依赖正常执行到最后。
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path


class LockHeldError(RuntimeError):
    """锁被另一个存活进程持有时抛出；holder 里是持有人信息（无任何密钥）。"""

    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(
            "another instance is running: pid={pid} host={host} since={started}".format(
                **{**{"pid": "?", "host": "?", "started": "?"}, **holder}))


def pid_alive(pid: int) -> bool:
    """跨平台判断进程是否存活。pid 无效或已退出返回 False。"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            # 中文 Windows 的 tasklist 输出是 GBK：显式容错解码
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="ignore", timeout=10).stdout
            return str(pid) in out.replace(",", "")
        os.kill(pid, 0)  # POSIX：进程不存在抛 ProcessLookupError
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True  # 无法判断时保守认为存活，避免误抢活锁


class InstanceLock:
    """用法： with InstanceLock(path): ...（异常退出也会释放）"""

    def __init__(self, path, max_age_seconds: int = 6 * 3600):
        self.path = Path(path)
        self.max_age_seconds = max_age_seconds
        self._fd = None

    # ── 内部 ─────────────────────────────────────────────
    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        info = {"pid": os.getpid(), "host": socket.gethostname(),
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ts": time.time()}
        os.write(fd, json.dumps(info).encode("utf-8"))
        os.close(fd)
        self._fd = True
        return True

    def _read_holder(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"pid": 0, "host": "unknown", "started": "unknown", "ts": 0}

    def _stale(self, holder: dict) -> bool:
        if holder.get("pid") and not pid_alive(int(holder.get("pid") or 0)):
            return True                      # 崩溃残留：进程已死
        ts = float(holder.get("ts") or 0)
        if ts and time.time() - ts > self.max_age_seconds:
            return True                      # 过期残留
        return False

    # ── 对外 ─────────────────────────────────────────────
    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            return self
        holder = self._read_holder()
        if not self._stale(holder):
            raise LockHeldError(holder)
        # 残留锁接管：改名隔离后重新原子创建（并发接管只有一个能成功）
        stale_path = self.path.with_suffix(".stale")
        try:
            os.replace(self.path, stale_path)
        except FileNotFoundError:
            pass
        if self._try_create():
            return self
        # 接管竞争失败 = 别的活进程刚拿到，按正常占用处理
        raise LockHeldError(self._read_holder())

    def release(self):
        if self._fd:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
