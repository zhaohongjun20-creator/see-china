"""主题级状态存储 + 视频资产/数据快照（数据反馈闭环）。

videos    —— 咱们产出的视频资产（bvid/标题/频道）
snapshots —— 每日播放量快照（MatrixMedia 式数据回读）
"""
import sqlite3, os, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    topic       TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    status      TEXT DEFAULT 'dryrun',   -- dryrun|published|failed:...
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS videos (
    bvid        TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    channel     TEXT DEFAULT '',         -- 军史|风景|技术|手动
    published_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    bvid        TEXT,
    date        TEXT,                    -- YYYY-MM-DD
    plays       INTEGER DEFAULT 0,
    PRIMARY KEY (bvid, date)
);
"""


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)

    # ── 主题 ──────────────────────────────────────────────
    def is_topic_done(self, topic: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM topics WHERE topic=?", (topic,)).fetchone()
        return bool(row and row[0] == "published")

    def mark_topic(self, topic: str, title: str, status: str):
        self.conn.execute(
            "INSERT INTO topics (topic, title, status, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(topic) DO UPDATE SET title=excluded.title, status=excluded.status",
            (topic, title, status,
             datetime.datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    # ── 视频资产 / 快照 ──────────────────────────────────
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

    def prev_plays(self, bvid: str, before_date: str) -> int | None:
        """before_date 之前最近一次快照的播放数（无则 None=新视频）。"""
        row = self.conn.execute(
            "SELECT plays FROM snapshots WHERE bvid=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (bvid, before_date)).fetchone()
        return row[0] if row else None
