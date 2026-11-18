"""SQLite 存储层。

关键不变式:
- 某一版的完整快照(entries)+ 原始操作(raw_ops)+ 版本登记(versions)
  + 当前版指针(hours.current_version)在同一个事务里提交,因此失败或重启都
  不会暴露"半版"数据,原始操作轨迹也不会出现孤儿记录。
- 未确认版本的页面不落库,采集中的残页只存在于内存,重试时整版重取。
- raw_ops 只如实记录源站实际发布的操作(含同值 upsert),绝不根据快照差额补造;
  升级前已确认的老版本没有 raw_ops 行,查询轨迹时按"来源未留存"明确标识。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/collector.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hours (
    hour            TEXT PRIMARY KEY,          -- UTC 小时,如 2026-11-18T10
    current_version INTEGER NOT NULL DEFAULT 0, -- 当前版指针:0 表示尚无确认版
    status          TEXT NOT NULL DEFAULT 'idle',
    pinned_version  INTEGER,
    last_error      TEXT,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    hour           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_pages    INTEGER NOT NULL,
    item_count     INTEGER NOT NULL,
    station_count  INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL,
    PRIMARY KEY (hour, version)
);
CREATE TABLE IF NOT EXISTS entries (
    hour       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    pm25       REAL NOT NULL,
    PRIMARY KEY (hour, version, station_id)
);
CREATE TABLE IF NOT EXISTS raw_ops (
    -- 源站实际发布的原始操作记录(逐页逐序保存,含同值 upsert)
    hour        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    seq         INTEGER NOT NULL,          -- 该版内操作顺序,从 1 开始
    station_id  TEXT NOT NULL,
    op          TEXT NOT NULL,             -- upsert / delete
    pm25        REAL,                       -- delete 时为 NULL
    before_pm25 REAL,                       -- 操作前值:该站此前不在快照中则 NULL
    after_pm25  REAL,                       -- 操作后值:delete 后为 NULL
    reason_code INTEGER,                    -- 源站原因编号;源站未给则 NULL
    page        INTEGER NOT NULL,          -- 操作来自源站哪一页
    PRIMARY KEY (hour, version, seq)
);
CREATE INDEX IF NOT EXISTS idx_raw_ops_hour_station
    ON raw_ops (hour, station_id, version, seq);
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # 自动提交,显式事务见 tx()
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    db = connect()
    try:
        db.executescript(SCHEMA)
        # 重启清理:把中断在 collecting 的小时复位为 idle。
        # 数据本身由事务保证完整,这里只修正状态元信息。
        db.execute("UPDATE hours SET status='idle' WHERE status='collecting'")
    finally:
        db.close()


@contextmanager
def tx(db: sqlite3.Connection):
    """显式事务:要么整体提交,要么整体回滚。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except Exception:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def ensure_hour(db: sqlite3.Connection, hour: str, now: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO hours(hour, current_version, status, updated_at) "
        "VALUES (?, 0, 'idle', ?)",
        (hour, now),
    )


def get_hour(db: sqlite3.Connection, hour: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM hours WHERE hour=?", (hour,)).fetchone()


def snapshot(db: sqlite3.Connection, hour: str, version: int) -> dict[str, float]:
    rows = db.execute(
        "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=?",
        (hour, version),
    ).fetchall()
    return {r["station_id"]: r["pm25"] for r in rows}
