# -*- coding: utf-8 -*-
import sqlite3

from src.store import (FAILED, GENERATING, PENDING, PUBLISHED, READY, REVIEW,
                       Store)


def test_claim_is_atomic_across_connections(tmp_path):
    db = str(tmp_path / "t.db")
    s1, s2 = Store(db), Store(db)
    assert s1.claim_topic("A") is True
    assert s2.claim_topic("A") is False        # 同一选题不会被重复领取


def test_ready_and_published_both_done(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.mark_ready("A", "TA")
    s.mark_published("B")
    assert s.is_topic_done("A") and s.is_topic_done("B")
    assert s.topic_row("A")["title"] == "TA"
    assert s.pending_topics(["A", "B", "C"]) == ["C"]


def test_failed_retry_limit_and_reset(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    for _ in range(3):
        assert s.claim_topic("A") is True
        s.mark_failed("A", "script", "boom")
    row = s.topic_row("A")
    assert row["status"] == FAILED and row["retries"] == 3
    assert "A" not in s.pending_topics(["A"], max_retries=3)
    assert s.claim_topic("A") is False          # 重试耗尽不再自动领取
    s.reset_topic("A")
    assert s.topic_row("A")["retries"] == 0
    assert s.claim_topic("A") is True


def test_review_blocks_auto_flow(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.mark_review("A", "T")
    assert s.pending_topics(["A"]) == []
    assert s.claim_topic("A") is False          # 待人工裁决期间不可自动领取


def test_reclaim_stuck_generating(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.claim_topic("A")                           # → generating
    assert s.claim_topic("A") is False           # generating 不可直接再领
    assert s.reclaim_stuck("A") is True          # 崩溃残留显式恢复
    s.mark_ready("A")
    assert s.reclaim_stuck("A") is False         # 非 generating 状态不适用


def test_legacy_migration(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE topics (topic TEXT PRIMARY KEY, title TEXT DEFAULT '',
        status TEXT DEFAULT 'dryrun', created_at TEXT);
      CREATE TABLE videos (bvid TEXT PRIMARY KEY, title TEXT DEFAULT '',
        channel TEXT DEFAULT '', published_at TEXT DEFAULT '');
      CREATE TABLE snapshots (bvid TEXT, date TEXT, plays INTEGER DEFAULT 0,
        PRIMARY KEY (bvid, date));
    """)
    conn.executemany(
        "INSERT INTO topics (topic, status) VALUES (?, ?)",
        [("a", "done"), ("b", "published"), ("c", "failed:script:boom"),
         ("d", "dryrun")])
    conn.commit()
    conn.close()

    s = Store(str(db))                           # 打开即增量迁移
    rows = {r["topic"]: r for r in s.all_topic_rows()}
    assert rows["a"]["status"] == READY
    assert rows["b"]["status"] == PUBLISHED
    assert rows["c"]["status"] == FAILED
    assert rows["c"]["fail_reason"] == "script:boom"
    assert rows["d"]["status"] == PENDING
    # 历史数据未丢失
    assert s.videos_all() == [] and s.prev_plays("x", "2020-01-01") is None
