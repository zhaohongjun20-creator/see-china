# -*- coding: utf-8 -*-
"""锁测试（第四轮整改后）：OS 级锁的多进程真并发验证。

评审要求：多进程同时争抢同一把锁，最多一个进程进入临界区；
活进程的锁不被接管；崩溃遗留锁可安全恢复。
子进程结果写结果文件（而非 stdout），避免读取时序造成的测试抖动。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.locks import InstanceLock, LockHeldError

ROOT = Path(__file__).resolve().parents[1]

# 一次性尝试：拿到锁就一直持有（由父进程杀掉，模拟活进程/崩溃两种结局）
_WORKER_ONE_SHOT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from src.locks import InstanceLock, LockHeldError
try:
    lk = InstanceLock(sys.argv[2]).acquire()
    res = {"win": True, "pid": os.getpid()}
except LockHeldError:
    res = {"win": False, "pid": os.getpid()}
with open(os.path.join(sys.argv[3], "result_%d.json" % os.getpid()), "w") as f:
    json.dump(res, f)
if res["win"]:
    time.sleep(20)   # 持锁不放，由父进程决定后续
"""

# 限时持有：拿到锁持 hold 秒后正常释放退出（模拟长任务）
_WORKER_HOLD_TIMED = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from src.locks import InstanceLock
lk = InstanceLock(sys.argv[2]).acquire()
with open(os.path.join(sys.argv[3], "result_%d.json" % os.getpid()), "w") as f:
    json.dump({"win": True, "pid": os.getpid()}, f)
time.sleep(float(sys.argv[4]))
lk.release()
"""

# 竞争模式：反复尝试，拿到后持锁 0.4s 释放，记录持锁窗口
_WORKER_CONTENTION = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from src.locks import InstanceLock, LockHeldError
deadline = time.time() + 10
win, t0, t1 = False, None, None
while time.time() < deadline:
    try:
        lk = InstanceLock(sys.argv[2]).acquire()
    except LockHeldError:
        time.sleep(0.02)
    else:
        t0 = time.time()
        time.sleep(0.4)
        t1 = time.time()
        lk.release()
        win = True
        break
with open(os.path.join(sys.argv[3], "result_%d.json" % os.getpid()), "w") as f:
    json.dump({"win": win, "pid": os.getpid(), "t0": t0, "t1": t1}, f)
"""


def _spawn(worker, lock_path, result_dir, *extra):
    return subprocess.Popen(
        [sys.executable, "-c", worker, str(ROOT), str(lock_path), str(result_dir),
         *extra],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT))


def _wait_results(result_dir, n, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = sorted(result_dir.glob("result_*.json"))
        if len(files) >= n:
            break
        time.sleep(0.1)
    out = []
    for f in sorted(result_dir.glob("result_*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return out


# ── 基础语义（进程内） ──────────────────────────────────
def test_acquire_then_reacquire_after_release(tmp_path):
    lk = InstanceLock(tmp_path / "l.lock")
    lk.acquire()
    lk.release()
    InstanceLock(tmp_path / "l.lock").acquire().release()   # 释放后可再次获取


def test_second_instance_rejected_in_process(tmp_path):
    with InstanceLock(tmp_path / "l.lock"):
        with pytest.raises(LockHeldError) as ei:
            InstanceLock(tmp_path / "l.lock").acquire()
        assert ei.value.holder.get("pid") == os.getpid()


def test_released_on_exception(tmp_path):
    with pytest.raises(ValueError):
        with InstanceLock(tmp_path / "l.lock"):
            raise ValueError("boom")
    InstanceLock(tmp_path / "l.lock").acquire().release()   # 异常路径也释放


def test_legacy_lock_file_still_works(tmp_path):
    """旧版本残留的锁文件（纯 JSON、无 OS 锁）可以直接打开并加锁。"""
    (tmp_path / "l.lock").write_text(
        '{"pid": 0, "host": "old", "started": "yesterday"}', encoding="utf-8")
    InstanceLock(tmp_path / "l.lock").acquire().release()


# ── 多进程真并发（评审#4 核心要求） ─────────────────────
def test_concurrent_race_single_winner(tmp_path):
    """6 个进程同时争抢：最多一个进程能进入临界区（内核保证）。"""
    rdir = tmp_path / "results"
    rdir.mkdir()
    lock = tmp_path / "l.lock"
    procs = [_spawn(_WORKER_ONE_SHOT, lock, rdir) for _ in range(6)]
    try:
        lines = _wait_results(rdir, 6)
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()
    assert len(lines) == 6, f"some workers did not report: {lines}"
    winners = [x for x in lines if x["win"]]
    assert len(winners) == 1, f"双持锁！{lines}"


def test_mutual_exclusion_under_contention(tmp_path):
    """竞争模式下持锁窗口两两不重叠；确实发生了多次交接。"""
    rdir = tmp_path / "results"
    rdir.mkdir()
    lock = tmp_path / "l.lock"
    procs = [_spawn(_WORKER_CONTENTION, lock, rdir) for _ in range(6)]
    try:
        lines = _wait_results(rdir, 6)
    finally:
        for p in procs:
            p.kill()
            p.wait()
    assert len(lines) == 6
    winners = sorted([x for x in lines if x["win"]], key=lambda x: x["t0"])
    assert len(winners) >= 2, f"争抢未实际发生: {lines}"
    for a, b in zip(winners, winners[1:]):
        assert a["t1"] <= b["t0"] + 0.05, f"持锁窗口重叠（互斥被破坏）: {a} {b}"


def test_alive_holder_rejected_and_crash_recoverable(tmp_path):
    """活进程持锁 → 其他进程被拒绝；持锁进程被硬杀（无 release）→ 立即可恢复。"""
    rdir = tmp_path / "results"
    rdir.mkdir()
    lock = tmp_path / "l.lock"
    p = _spawn(_WORKER_ONE_SHOT, lock, rdir)
    try:
        lines = _wait_results(rdir, 1, timeout=30)
        assert lines and lines[0]["win"], lines   # 子进程已持锁
        with pytest.raises(LockHeldError):        # 活进程 → 拒绝接管
            InstanceLock(lock).acquire()
    finally:
        p.kill()
        p.wait()
    # 持有者被硬杀：内核自动释放，无需任何"过期接管"流程
    InstanceLock(lock).acquire().release()


# ── 第五轮评审新增用例 ──────────────────────────────────
def test_holder_info_contains_unique_token(tmp_path):
    """持有人信息含实例唯一 token（评审要求 2），且不同实例 token 不同。"""
    lock = tmp_path / "l.lock"
    a = InstanceLock(lock)
    a.acquire()
    info_a = json.loads(lock.read_text(encoding="utf-8"))
    assert info_a["token"] and info_a["pid"] == os.getpid()
    with pytest.raises(LockHeldError) as ei:
        InstanceLock(lock).acquire()
    # 被拒者读到的持有人 token 与持有者一致（诊断可追溯）
    assert ei.value.holder.get("token") == info_a["token"]
    a.release()


def test_concurrent_takeover_of_dead_residue(tmp_path):
    """多个进程同时接管同一把死进程残留锁：恰好一个获得生产权（评审要求 4）。"""
    rdir1, rdir2 = tmp_path / "r1", tmp_path / "r2"
    rdir1.mkdir()
    rdir2.mkdir()
    lock = tmp_path / "l.lock"
    # 制造死进程残留：持有者被硬杀，不释放、锁文件仍在
    victim = _spawn(_WORKER_ONE_SHOT, lock, rdir1)
    try:
        assert _wait_results(rdir1, 1, timeout=30)[0]["win"]
    finally:
        victim.kill()
        victim.wait()
    # 6 个进程同时争抢这把残留锁
    procs = [_spawn(_WORKER_ONE_SHOT, lock, rdir2) for _ in range(6)]
    try:
        lines = _wait_results(rdir2, 6)
    finally:
        for p in procs:
            p.kill()
            p.wait()
    assert len(lines) == 6
    assert len([x for x in lines if x["win"]]) == 1, f"双持锁！{lines}"


def test_race_on_corrupt_lock_file(tmp_path):
    """锁文件为空/JSON 损坏时并发恢复：互斥不破坏，恰好一个胜出（评审要求 3/4）。"""
    rdir = tmp_path / "results"
    rdir.mkdir()
    lock = tmp_path / "l.lock"
    lock.write_bytes(b"\x00\xffnot json{{{ truncated")   # 模拟写一半崩溃
    procs = [_spawn(_WORKER_ONE_SHOT, lock, rdir) for _ in range(6)]
    try:
        lines = _wait_results(rdir, 6)
    finally:
        for p in procs:
            p.kill()
            p.wait()
    assert len(lines) == 6
    assert len([x for x in lines if x["win"]]) == 1, f"双持锁！{lines}"
    # 胜者已把损坏内容覆写为合法持有人信息
    info = json.loads(lock.read_text(encoding="utf-8"))
    assert info["token"]


@pytest.mark.parametrize("junk", [b"", b"{corrupt", b"\x00\x01\x02"])
def test_incomplete_lock_file_recoverable(tmp_path, junk):
    """空文件/半截 JSON/二进制垃圾残留：单实例可直接恢复（评审要求 3）。"""
    lock = tmp_path / "l.lock"
    lock.write_bytes(junk)
    lk = InstanceLock(lock)
    lk.acquire()
    info = json.loads(lock.read_text(encoding="utf-8"))
    assert info["token"]                        # 残留已被覆写
    lk.release()


def test_stale_holder_release_cannot_remove_new_holders_lock(tmp_path):
    """旧持有者的（重复）释放不会删除/破坏新持有者的锁（评审要求 4）。"""
    lock = tmp_path / "l.lock"
    a = InstanceLock(lock)
    a.acquire()
    with pytest.raises(LockHeldError):
        InstanceLock(lock).acquire()
    a.release()                                  # A 正常释放
    b = InstanceLock(lock)
    b.acquire()                                  # B 成为新持有者
    a.release()                                  # A 的过期对象再次释放 → 必须是幂等空操作
    # B 的锁依然有效：C 被拒绝
    with pytest.raises(LockHeldError):
        InstanceLock(lock).acquire()
    assert lock.is_file()                        # B 的锁文件未被删除
    b.release()
    InstanceLock(lock).acquire().release()       # B 释放后一切如常


def test_long_task_holder_never_taken_over(tmp_path):
    """长任务保护（评审要求 5）：活进程持锁期间反复尝试均被拒绝，
    与持锁时长无关；正常释放后立刻可获取。"""
    rdir = tmp_path / "results"
    rdir.mkdir()
    lock = tmp_path / "l.lock"
    p = _spawn(_WORKER_HOLD_TIMED, lock, rdir, "3.0")
    try:
        assert _wait_results(rdir, 1, timeout=30)[0]["win"]   # 子进程已持锁
        for attempt in range(3):                 # 3 秒长任务窗口内持续被拒
            with pytest.raises(LockHeldError):
                InstanceLock(lock).acquire()
            time.sleep(0.8)
    finally:
        p.wait(timeout=60)                       # 子进程正常释放退出
    InstanceLock(lock).acquire().release()       # 释放后立即可获取
