# -*- coding: utf-8 -*-
import json
import subprocess
import sys
import time

import pytest

from src.locks import InstanceLock, LockHeldError


def test_acquire_release(tmp_path):
    lk = InstanceLock(tmp_path / "l.lock")
    lk.acquire()
    assert (tmp_path / "l.lock").is_file()
    lk.release()
    assert not (tmp_path / "l.lock").exists()
    # 释放后可再次获取
    InstanceLock(tmp_path / "l.lock").acquire().release()


def test_second_instance_rejected(tmp_path):
    with InstanceLock(tmp_path / "l.lock"):
        with pytest.raises(LockHeldError) as ei:
            InstanceLock(tmp_path / "l.lock").acquire()
        assert ei.value.holder.get("pid")
        assert "pid" in str(ei.value)


def test_released_on_exception(tmp_path):
    with pytest.raises(ValueError):
        with InstanceLock(tmp_path / "l.lock"):
            raise ValueError("boom")
    assert not (tmp_path / "l.lock").exists()


def _write_holder(path, pid, ts=None):
    path.write_text(json.dumps(
        {"pid": pid, "host": "test", "started": "now",
         "ts": ts if ts is not None else time.time()}), encoding="utf-8")


def test_alive_holder_rejected_then_takeover_after_death(tmp_path):
    path = tmp_path / "l.lock"
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)                     # 确保进程已启动
        _write_holder(path, p.pid)
        with pytest.raises(LockHeldError):
            InstanceLock(path).acquire()
    finally:
        p.terminate()
        p.wait()
    # 持有进程死亡 → 崩溃残留 → 自动接管
    lk = InstanceLock(path)
    lk.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] != p.pid
    lk.release()


def test_expired_lock_taken_over(tmp_path):
    path = tmp_path / "l.lock"
    _write_holder(path, 0, ts=time.time() - 7 * 3600)   # 过期残留
    lk = InstanceLock(path, max_age_seconds=6 * 3600)
    lk.acquire()
    lk.release()
