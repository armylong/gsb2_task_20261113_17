"""SQLite 数据访问层。

所有业务状态都持久化在该数据库中，采集引擎只通过本模块读写状态，
因此程序重启后可以从数据库恢复任务进度，继续未完成的采集。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    entry_url       TEXT NOT NULL,
    origin          TEXT NOT NULL,              -- 允许采集的源（协议://主机:端口）
    max_concurrency INTEGER NOT NULL DEFAULT 1, -- 该任务同时进行的请求数上限
    min_interval    REAL NOT NULL DEFAULT 1.0,  -- 同一网站两次访问最小间隔（秒）
    max_retries     INTEGER NOT NULL DEFAULT 3,
    fields_json     TEXT NOT NULL DEFAULT '[]', -- 字段选择配置 [{path,column}]
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    round_no        INTEGER NOT NULL,
    status          TEXT NOT NULL,              -- queued/running/paused/completed/failed
    pages_fetched   INTEGER NOT NULL DEFAULT 0,
    record_count    INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    pending_url     TEXT,                       -- 下一条待采集的页面地址
    error           TEXT,
    created_at      REAL NOT NULL,
    finished_at     REAL,
    UNIQUE(task_id, round_no)
);

-- 每一轮实际成功访问过的页面地址，用于翻页绕回检测
CREATE TABLE IF NOT EXISTS round_visited (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id  INTEGER NOT NULL REFERENCES rounds(id),
    url       TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    UNIQUE(round_id, url)
);

-- 每一轮的记录快照（按记录编号 upsert，重复 id 保留最后一次采到的内容）
CREATE TABLE IF NOT EXISTS round_records (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id  INTEGER NOT NULL REFERENCES rounds(id),
    record_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(round_id, record_id)
);

-- 终止一轮采集的失败请求（404/403、重试耗尽、外站跳转等）
CREATE TABLE IF NOT EXISTS round_failures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id  INTEGER NOT NULL REFERENCES rounds(id),
    url       TEXT NOT NULL,
    status_code INTEGER,
    attempts  INTEGER NOT NULL DEFAULT 1,
    reason    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Database:
    """线程安全的 SQLite 封装：单连接 + 串行锁 + WAL。"""

    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---- 通用 ----
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- 任务 ----
    def create_task(self, name: str, entry_url: str, origin: str,
                    max_concurrency: int, min_interval: float,
                    max_retries: int, fields: list[dict]) -> int:
        cur = self.execute(
            "INSERT INTO tasks(name, entry_url, origin, max_concurrency, "
            "min_interval, max_retries, fields_json, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, entry_url, origin, max_concurrency, min_interval,
             max_retries, json.dumps(fields, ensure_ascii=False), "queued", time.time()),
        )
        return int(cur.lastrowid)

    def get_task(self, task_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT t.*, "
            " (SELECT COUNT(*) FROM rounds r WHERE r.task_id=t.id) AS round_count, "
            " (SELECT MAX(round_no) FROM rounds r WHERE r.task_id=t.id) AS last_round_no, "
            " (SELECT r2.pages_fetched FROM rounds r2 WHERE r2.task_id=t.id "
            "   ORDER BY r2.round_no DESC LIMIT 1) AS last_pages, "
            " (SELECT r3.record_count FROM rounds r3 WHERE r3.task_id=t.id "
            "   ORDER BY r3.round_no DESC LIMIT 1) AS last_records, "
            " (SELECT r4.retry_count FROM rounds r4 WHERE r4.task_id=t.id "
            "   ORDER BY r4.round_no DESC LIMIT 1) AS last_retries "
            "FROM tasks t ORDER BY t.id DESC"
        )

    def update_task_status(self, task_id: int, status: str) -> None:
        self.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def update_task_fields(self, task_id: int, fields: list[dict]) -> None:
        self.execute("UPDATE tasks SET fields_json=? WHERE id=?",
                     (json.dumps(fields, ensure_ascii=False), task_id))

    # ---- 轮次 ----
    def create_round(self, task_id: int, pending_url: str) -> int:
        row = self.query_one(
            "SELECT COALESCE(MAX(round_no),0)+1 AS next_no FROM rounds WHERE task_id=?",
            (task_id,),
        )
        round_no = int(row["next_no"])
        cur = self.execute(
            "INSERT INTO rounds(task_id, round_no, status, pending_url, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, round_no, "queued", pending_url, time.time()),
        )
        return int(cur.lastrowid)

    def get_round(self, round_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM rounds WHERE id=?", (round_id,))

    def list_rounds(self, task_id: int) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM rounds WHERE task_id=? ORDER BY round_no DESC", (task_id,)
        )

    def get_latest_completed_round(self, task_id: int,
                                   before_round: Optional[int] = None) -> Optional[sqlite3.Row]:
        if before_round is None:
            return self.query_one(
                "SELECT * FROM rounds WHERE task_id=? AND status='completed' "
                "ORDER BY round_no DESC LIMIT 1", (task_id,))
        return self.query_one(
            "SELECT * FROM rounds WHERE task_id=? AND status='completed' AND round_no<? "
            "ORDER BY round_no DESC LIMIT 1", (task_id, before_round))

    def update_round(self, round_id: int, **cols: Any) -> None:
        if not cols:
            return
        keys = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE rounds SET {keys} WHERE id=?",
                     (*cols.values(), round_id))

    def bump_round_stats(self, round_id: int, *, pages: int = 0,
                         retries: int = 0) -> None:
        self.execute(
            "UPDATE rounds SET pages_fetched=pages_fetched+?, "
            "retry_count=retry_count+? WHERE id=?",
            (pages, retries, round_id),
        )

    def recount_round_records(self, round_id: int) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM round_records WHERE round_id=?", (round_id,))
        count = int(row["c"])
        self.execute("UPDATE rounds SET record_count=? WHERE id=?", (count, round_id))
        return count

    # ---- 已访问页面 ----
    def mark_visited(self, round_id: int, url: str) -> bool:
        """返回 True 表示新地址；False 表示该地址本轮已访问过（绕回）。"""
        try:
            self.execute(
                "INSERT INTO round_visited(round_id, url, fetched_at) VALUES (?,?,?)",
                (round_id, url, time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def is_visited(self, round_id: int, url: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM round_visited WHERE round_id=? AND url=? LIMIT 1",
            (round_id, url),
        )
        return row is not None

    # ---- 记录 ----
    def upsert_record(self, round_id: int, record_id: str, data: Any) -> None:
        # 同一轮同一 id：后采到的覆盖先采到的（保留最后一次成功内容）
        self.execute(
            "INSERT INTO round_records(round_id, record_id, data_json, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(round_id, record_id) DO UPDATE SET "
            "data_json=excluded.data_json, updated_at=excluded.updated_at",
            (round_id, str(record_id), json.dumps(data, ensure_ascii=False), time.time()),
        )

    def list_records(self, round_id: int, limit: int, offset: int) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM round_records WHERE round_id=? "
            "ORDER BY id LIMIT ? OFFSET ?", (round_id, limit, offset))

    def get_record_history(self, task_id: int, record_id: str) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT rr.*, r.round_no, r.status AS round_status FROM round_records rr "
            "JOIN rounds r ON r.id=rr.round_id "
            "WHERE r.task_id=? AND rr.record_id=? ORDER BY r.round_no",
            (task_id, str(record_id)),
        )

    # ---- 失败请求 ----
    def add_failure(self, round_id: int, url: str,
                    status_code: Optional[int], attempts: int, reason: str) -> None:
        self.execute(
            "INSERT INTO round_failures(round_id, url, status_code, attempts, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (round_id, url, status_code, attempts, reason, time.time()),
        )

    def list_failures(self, round_id: int) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM round_failures WHERE round_id=? ORDER BY id", (round_id,))
