# -*- coding: utf-8 -*-
"""选题状态机存储（P0-2 统一状态流转）。

状态机：
    pending ──claim──> generating ──> review ──> ready ──(人工确认上传)──> published
       ^                   │            │
       │                   ▼            ▼ (AI 审核未通过, 等人工)
       └──── 重试 <──── failed <──────┘

- "成片完成"(ready) 与 "已发布"(published) 严格分开：本产线成片需人工上传。
- is_topic_done()：ready/published 均视为"已完成制作"，重新运行时默认跳过。
- claim_topic()：单条 UPDATE 原子领取，两个进程同时启动不会重复领到同一选题。
- failed 记录失败阶段/原因/重试次数；超过上限不再自动重试（可 --redo 重置）。
- 旧库兼容：只增量迁移（加列 + 状态映射），不删表重建，历史数据全部保留。

videos / snapshots 表保持原样（数据回流闭环用）。
"""
import datetime
import os
import sqlite3

# 统一状态常量
PENDING = "pending"
GENERATING = "generating"
REVIEW = "review"
READY = "ready"
PUBLISHED = "published"
FAILED = "failed"

VALID_STATUSES = {PENDING, GENERATING, REVIEW, READY, PUBLISHED, FAILED}
# 视为"制作已完成、默认跳过"的状态集合
DONE_STATUSES = {READY, PUBLISHED}
# 可被领取（含崩溃后遗留的 generating——单实例锁保证无并发写）
CLAIMABLE = {PENDING, FAILED, GENERATING}

# 旧版 status 取值 → 新状态机
_LEGACY_MAP = {"dryrun": PENDING, "done": READY}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    topic       TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',
    fail_stage  TEXT DEFAULT '',
    fail_reason TEXT DEFAULT '',
    retries     INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS videos (
    bvid        TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    channel     TEXT DEFAULT '',
    published_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    bvid        TEXT,
    date        TEXT,
    plays       INTEGER DEFAULT 0,
    PRIMARY KEY (bvid, date)
);
"""

# 兼容旧库（缺列则补）——绝不 DROP/RENAME，增量迁移。
# 注意：ALTER TABLE 的 DEFAULT 必须是恒量（SQLite 限制），时间戳由代码写入。
_MIGRATE_COLUMNS = {
    "topics": [("fail_stage", "TEXT DEFAULT ''"),
               ("fail_reason", "TEXT DEFAULT ''"),
               ("retries", "INTEGER DEFAULT 0"),
               ("updated_at", "TEXT DEFAULT ''")],
}


class Store:
    def __init__(self, db_path: str):
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._migrate_legacy_statuses()

    # ── 迁移 ─────────────────────────────────────────────
    def _ensure_columns(self):
        for table, cols in _MIGRATE_COLUMNS.items():
            existing = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def _migrate_legacy_statuses(self):
        """旧状态值映射到新状态机；failed:xxx 保留原因进 fail_reason。"""
        rows = self.conn.execute("SELECT topic, status, title FROM topics").fetchall()
        for r in rows:
            st = (r["status"] or "").strip()
            if st in VALID_STATUSES:
                continue
            if st.startswith("failed:"):
                new, reason = FAILED, st[len("failed:"):]
                self.conn.execute(
                    "UPDATE topics SET status=?, fail_stage='legacy', fail_reason=? "
                    "WHERE topic=?", (new, reason, r["topic"]))
            else:
                new = _LEGACY_MAP.get(st, PENDING)
                self.conn.execute(
                    "UPDATE topics SET status=? WHERE topic=?", (new, r["topic"]))
        self.conn.commit()

    # ── 选题状态机 ────────────────────────────────────────
    def topic_row(self, topic: str):
        return self.conn.execute(
            "SELECT * FROM topics WHERE topic=?", (topic,)).fetchone()

    def is_topic_done(self, topic: str) -> bool:
        """成片完成或已发布 → 默认跳过（不再自动重做）。"""
        row = self.topic_row(topic)
        return bool(row and row["status"] in DONE_STATUSES)

    def claim_topic(self, topic: str, max_retries: int = 3) -> bool:
        """原子领取选题。成功=True；已完成/重试耗尽/被人领走=False。

        只领 pending/failed：两个连接同时领取同一 pending 选题时，
        UPDATE 的 WHERE 保证恰好一个成功（rowcount==1）。
        generating 是崩溃残留状态，走 reclaim_stuck() 显式恢复。
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO topics (topic, status) VALUES (?, ?)",
            (topic, PENDING))
        row = self.topic_row(topic)
        if row["status"] in DONE_STATUSES:
            return False
        if row["status"] == FAILED and row["retries"] >= max_retries:
            return False
        cur = self.conn.execute(
            "UPDATE topics SET status=?, updated_at=? "
            "WHERE topic=? AND status IN (?,?)",
            (GENERATING, _now(), topic, PENDING, FAILED))
        self.conn.commit()
        return cur.rowcount == 1

    def reclaim_stuck(self, topic: str) -> bool:
        """崩溃残留恢复：generating → generating（刷新时间）。仅持锁后调用。"""
        cur = self.conn.execute(
            "UPDATE topics SET status=?, updated_at=? "
            "WHERE topic=? AND status=?",
            (GENERATING, _now(), topic, GENERATING))
        self.conn.commit()
        return cur.rowcount == 1

    def mark_generating(self, topic: str, title: str = ""):
        self._mark(topic, GENERATING, title)

    def mark_review(self, topic: str, title: str = ""):
        self._mark(topic, REVIEW, title)

    def mark_ready(self, topic: str, title: str = ""):
        self._mark(topic, READY, title)

    def mark_published(self, topic: str):
        self._mark(topic, PUBLISHED)

    def mark_failed(self, topic: str, stage: str, reason: str):
        self.conn.execute(
            "UPDATE topics SET status=?, fail_stage=?, fail_reason=?, "
            "retries=retries+1, updated_at=? WHERE topic=?",
            (FAILED, stage, reason[:2000], _now(), topic))
        self.conn.commit()

    def reset_topic(self, topic: str):
        """--redo：回到 pending 并清零重试计数。"""
        self.conn.execute(
            "UPDATE topics SET status=?, fail_stage='', fail_reason='', retries=0, "
            "updated_at=? WHERE topic=?", (PENDING, _now(), topic))
        self.conn.commit()

    def pending_topics(self, all_topics: list, max_retries: int = 3) -> list:
        """候选池：未做过、且失败未超上限的选题（失败选题允许重试，不永久占用）。"""
        out = []
        for t in all_topics:
            row = self.topic_row(t)
            if row is None:
                out.append(t)
            elif row["status"] in CLAIMABLE and not (
                    row["status"] == FAILED and row["retries"] >= max_retries):
                out.append(t)
        return out

    def all_topic_rows(self) -> list:
        return self.conn.execute(
            "SELECT * FROM topics ORDER BY updated_at DESC").fetchall()

    def _mark(self, topic: str, status: str, title: str = ""):
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        now = _now()
        self.conn.execute(
            "INSERT INTO topics (topic, title, status, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(topic) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at, "
            "title=CASE WHEN excluded.title!='' THEN excluded.title ELSE topics.title END",
            (topic, title or "", status, now))
        self.conn.commit()

    # ── 视频资产 / 快照（原样保留） ───────────────────────
    def record_video(self, bvid: str, title: str, channel: str,
                     published_at: str = ""):
        self.conn.execute(
            "INSERT INTO videos (bvid,title,channel,published_at) VALUES (?,?,?,?) "
            "ON CONFLICT(bvid) DO UPDATE SET title=excluded.title, channel=excluded.channel",
            (bvid, title, channel,
             published_at or datetime.date.today().isoformat()))
        self.conn.commit()

    def upsert_snapshot(self, bvid: str, plays: int, date: str = None):
        self.conn.execute(
            "INSERT INTO snapshots (bvid,date,plays) VALUES (?,?,?) "
            "ON CONFLICT(bvid,date) DO UPDATE SET plays=excluded.plays",
            (bvid, date or datetime.date.today().isoformat(), int(plays)))
        self.conn.commit()

    def videos_all(self) -> list:
        return self.conn.execute(
            "SELECT bvid,title,channel FROM videos").fetchall()

    def prev_plays(self, bvid: str, before_date: str):
        row = self.conn.execute(
            "SELECT plays FROM snapshots WHERE bvid=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (bvid, before_date)).fetchone()
        return row["plays"] if row else None


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
