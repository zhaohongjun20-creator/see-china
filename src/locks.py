# -*- coding: utf-8 -*-
"""单实例锁（第四轮整改：OS 级文件锁，从根本上消除接管竞态）。

评审#4 指出旧实现"改名隔离 + 重建"的接管流程存在竞态：两个进程可能同时
判定同一把锁失效，后到者会把先到者刚创建的新锁再次改名，导致双持锁。

本版不再有任何"接管"动作 —— 改用操作系统自带的文件区域锁：
- Windows 用 msvcrt.locking，POSIX 用 fcntl.flock，都是内核原子操作；
- 持有进程崩溃/被杀时内核自动释放 —— 不存在"残留锁"，无需接管、无需心跳；
- 活进程持锁期间，其他进程的非阻塞加锁必然失败（活进程保护）；
- 同一时刻最多一个进程持锁由内核保证，与重试/超时/锁龄参数无关。

锁文件只作为汇合点与诊断信息载体（pid/host/started），不承载互斥语义；
旧版本遗留的锁文件可被直接打开并加锁，无需迁移。
"""
import json
import os
import socket
import time
from pathlib import Path

try:    # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

try:    # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class LockHeldError(RuntimeError):
    """锁被另一个存活进程持有时抛出；holder 为持有人诊断信息（无密钥）。"""

    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(
            "another instance is running: pid={pid} host={host} since={started}".format(
                **{**{"pid": "?", "host": "?", "started": "?"}, **holder}))


class InstanceLock:
    """用法： with InstanceLock(path): ...

    非阻塞尝试获取 OS 文件锁；被占用时立即抛 LockHeldError（不等待、不接管）。
    """

    #: 锁区域放在离内容 1MiB 处：Windows 的区域锁会拒绝其他句柄读取被锁字节，
    #: 锁在高位就能让持有人诊断信息（文件头部）始终可读，互斥语义不变。
    LOCK_OFFSET = 1 << 20

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    # ── 平台原语 ─────────────────────────────────────────
    @staticmethod
    def _try_lock(fd: int) -> bool:
        """非阻塞加锁 LOCK_OFFSET 处的 1 字节区域。成功 True；被占 False。"""
        if msvcrt is not None:
            try:
                os.lseek(fd, InstanceLock.LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        raise RuntimeError("no OS file-lock primitive available on this platform")

    @staticmethod
    def _unlock(fd: int):
        try:
            if msvcrt is not None:
                os.lseek(fd, InstanceLock.LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # close(fd) 时内核兜底释放

    # ── 诊断信息 ─────────────────────────────────────────
    def _write_holder(self):
        info = {"pid": os.getpid(), "host": socket.gethostname(),
                "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            payload = (json.dumps(info) + "\n").encode("utf-8")
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, payload)
            os.ftruncate(self._fd, len(payload))
        except OSError:
            pass  # 诊断信息写失败不影响互斥

    def _read_holder(self) -> dict:
        """读持有人信息；短暂重试规避写入中的半截 JSON；失败给占位值。"""
        for _ in range(5):
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.02)
        return {"pid": 0, "host": "unknown", "started": "unknown"}

    # ── 对外 ─────────────────────────────────────────────
    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT)
        if not self._try_lock(fd):
            os.close(fd)
            raise LockHeldError(self._read_holder())
        self._fd = fd
        self._write_holder()
        return self

    def release(self):
        if self._fd is None:
            return
        try:
            self._unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
