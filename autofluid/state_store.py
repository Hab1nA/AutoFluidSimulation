"""状态库实现（SQLite）。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .constants import PIPELINE_STEPS, StepName, StepStatus, downstream_steps
from .models import ConfigRow
from .utils import ensure_dir, now_iso


class StateStore:
    """SQLite 状态库封装，支持断点续传。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        ensure_dir(str(Path(db_path).parent))
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS configs (
                    name TEXT PRIMARY KEY,
                    param1 REAL,
                    param2 REAL,
                    param3 REAL,
                    param4 REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS step_states (
                    name TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (name, step)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    def initialize_configs(self, configs: Iterable[ConfigRow]) -> None:
        """初始化构型与步骤状态。"""

        with self._lock, self._connect() as conn:
            for config in configs:
                conn.execute(
                    "INSERT OR IGNORE INTO configs (name, param1, param2, param3, param4) VALUES (?, ?, ?, ?, ?)",
                    (config.name, config.param1, config.param2, config.param3, config.param4),
                )
                for step in PIPELINE_STEPS:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO step_states (name, step, status, message, updated_at)
                        VALUES (?, ?, ?, '', ?)
                        """,
                        (config.name, step.value, StepStatus.WAITING.value, now_iso()),
                    )

    def get_configs(self) -> List[ConfigRow]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT name, param1, param2, param3, param4 FROM configs").fetchall()
        return [
            ConfigRow(
                name=row["name"],
                param1=row["param1"],
                param2=row["param2"],
                param3=row["param3"],
                param4=row["param4"],
            )
            for row in rows
        ]

    def get_step_status(self, name: str, step: StepName) -> StepStatus:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM step_states WHERE name = ? AND step = ?",
                (name, step.value),
            ).fetchone()
        if not row:
            return StepStatus.WAITING
        return StepStatus(row["status"])

    def update_step_status(self, name: str, step: StepName, status: StepStatus, message: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE step_states
                SET status = ?, message = ?, updated_at = ?
                WHERE name = ? AND step = ?
                """,
                (status.value, message, now_iso(), name, step.value),
            )

    def get_all_statuses(self) -> Dict[str, Dict[StepName, StepStatus]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT name, step, status FROM step_states").fetchall()
        result: Dict[str, Dict[StepName, StepStatus]] = {}
        for row in rows:
            name = row["name"]
            step = StepName(row["step"])
            result.setdefault(name, {})[step] = StepStatus(row["status"])
        return result

    def all_completed(self, step: StepName) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS pending FROM step_states WHERE step = ? AND status != ?",
                (step.value, StepStatus.COMPLETED.value),
            ).fetchone()
        return row["pending"] == 0

    def reset_steps(self, name: str, start_step: StepName) -> None:
        """重置指定构型的指定步骤及下游步骤。"""

        steps = downstream_steps(start_step)
        with self._lock, self._connect() as conn:
            for step in steps:
                conn.execute(
                    """
                    UPDATE step_states
                    SET status = ?, message = '', updated_at = ?
                    WHERE name = ? AND step = ?
                    """,
                    (StepStatus.WAITING.value, now_iso(), name, step.value),
                )

    def reset_all(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE step_states SET status = ?, message = '', updated_at = ?",
                (StepStatus.WAITING.value, now_iso()),
            )

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return row["value"]
