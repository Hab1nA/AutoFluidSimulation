"""
===============================================================================
共享状态管理器 (Shared State Manager)
基于 SQLite 的持久化状态存储，支持并发访问（WAL 模式）。
Daemon 写入状态，TUI 客户端读取状态。通过 IPC 命令触发状态变更。

数据库表结构：
- configs: 构型列表（从 Excel 读取后初始化）
- steps: 每个构型在每个步骤的状态
- engine_state: 引擎全局状态（running/paused/stopped）
===============================================================================
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from engine.config import (
    STEP_NAMES, STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING, STATUS_UNKNOWN_REMOTE,
    STATUS_COMPLETED, STATUS_ERROR,
    ALL_STATUSES, IPC_CONFIG,
    DEFAULT_WORKSTATION_ID,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


class StateManager:
    """
    共享状态管理器。

    使用 SQLite WAL 模式，允许多个进程同时读取（TUI 客户端），
    写入操作由 Daemon 进程独占（通过 Python 线程锁保护）。
    """

    def __init__(self, db_path: str | None = None):
        """
        初始化状态管理器。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path or IPC_CONFIG["db_path"]
        self._lock = threading.Lock()  # 线程安全锁
        self._config_pragmas()  # 首次初始化 PRAGMA 配置
        self._init_database()

    def _config_pragmas(self):
        """配置数据库级 PRAGMA 设置（仅初始化一次）。

        注：仅 journal_mode 是数据库级持久化的，其他 PRAGMA（synchronous、
        busy_timeout、foreign_keys）是每连接设置，需在 _get_connection() 中重复。
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式：读写并发（数据库级持久化）
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    @contextmanager
    def _get_connection(self, readonly: bool = False):
        """获取数据库连接（上下文管理器，自动提交/关闭）。

        每个新连接设置必要的 per-connection PRAGMA（journal_mode 由
        _config_pragmas() 在数据库级持久化，无需重复设置）。

        Args:
            readonly: 若为 True，跳过 commit（适用于纯查询操作，减少 I/O 开销）
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception:
            if not readonly:
                try:
                    conn.rollback()
                except sqlite3.Error as e:
                    logger.error("数据库回滚异常: %s", e)
            raise
        finally:
            try:
                conn.close()
            except sqlite3.Error as e:
                logger.error("数据库连接关闭异常: %s", e)

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
        """Return whether a table has a column."""
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_sql: str,
    ) -> None:
        """Add a column to an existing SQLite table if it is missing."""
        if not self._column_exists(conn, table_name, column_name):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    @staticmethod
    def _create_remote_tasks_table(conn: sqlite3.Connection) -> None:
        """Create the current remote_tasks table shape."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS remote_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workstation_id TEXT NOT NULL DEFAULT 'default',
                config_name INTEGER NOT NULL,
                step_name TEXT NOT NULL,
                task_name TEXT NOT NULL,
                flag_file TEXT NOT NULL,
                error_flag_file TEXT NOT NULL,
                log_file TEXT,
                pid_file TEXT,
                script_file TEXT,
                started_at REAL NOT NULL,
                UNIQUE(workstation_id, config_name, step_name)
            )
        """)

    @staticmethod
    def _remote_tasks_unique_has_workstation(conn: sqlite3.Connection) -> bool:
        """Return whether remote_tasks has the workstation-aware unique key."""
        for index in conn.execute("PRAGMA index_list(remote_tasks)").fetchall():
            is_unique = bool(index["unique"])
            if not is_unique:
                continue
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
            ]
            if columns == ["workstation_id", "config_name", "step_name"]:
                return True
        return False

    def _ensure_remote_tasks_schema(self, conn: sqlite3.Connection) -> None:
        """Migrate remote_tasks to the workstation-aware unique key."""
        self._create_remote_tasks_table(conn)
        self._ensure_column(
            conn,
            "remote_tasks",
            "workstation_id",
            "TEXT NOT NULL DEFAULT 'default'",
        )
        if self._remote_tasks_unique_has_workstation(conn):
            return

        old_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(remote_tasks)").fetchall()
        }
        conn.execute("ALTER TABLE remote_tasks RENAME TO remote_tasks_legacy")
        self._create_remote_tasks_table(conn)
        workstation_expr = (
            "COALESCE(workstation_id, 'default')"
            if "workstation_id" in old_columns
            else "'default'"
        )
        conn.execute(f"""
            INSERT OR REPLACE INTO remote_tasks (
                workstation_id, config_name, step_name, task_name, flag_file,
                error_flag_file, log_file, pid_file, script_file, started_at
            )
            SELECT {workstation_expr}, config_name, step_name, task_name, flag_file,
                   error_flag_file, log_file, pid_file, script_file, started_at
            FROM remote_tasks_legacy
        """)
        conn.execute("DROP TABLE remote_tasks_legacy")

    def _init_database(self):
        """初始化数据库表结构。"""
        with self._get_connection() as conn:
            # 构型列表表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS configs (
                    config_name INTEGER PRIMARY KEY,
                    param1 REAL NOT NULL,
                    param2 REAL NOT NULL,
                    param3 REAL NOT NULL,
                    param4 REAL NOT NULL
                )
            """)

            # 步骤状态表：每个构型的每个步骤一条记录
            conn.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_name INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Waiting',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    workstation_id TEXT DEFAULT NULL,
                    slot_id INTEGER DEFAULT NULL,
                    UNIQUE(config_name, step_name),
                    FOREIGN KEY(config_name) REFERENCES configs(config_name)
                )
            """)
            self._ensure_column(conn, "steps", "workstation_id", "TEXT DEFAULT NULL")
            self._ensure_column(conn, "steps", "slot_id", "INTEGER DEFAULT NULL")

            # 引擎全局状态表（单行记录）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engine_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # 远程后台任务元数据。用于 Daemon 重启后恢复 Meshing/Solver
            # 计划任务，避免状态仍为 Running 但内存映射丢失时重复启动 Fluent。
            self._ensure_remote_tasks_schema(conn)

            # 初始化引擎状态默认值
            defaults = {
                "engine_status": "stopped",   # stopped | running | paused
                "sw_macro_started": "false",   # SW 宏是否已启动
                "global_barrier_met": "false", # 全局屏障是否已通过
                "error_count": "0",
            }
            for key, val in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO engine_state (key, value) VALUES (?, ?)",
                    (key, val)
                )

            # 创建索引加速查询
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_config ON steps(config_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_remote_tasks_step "
                "ON remote_tasks(step_name)"
            )

        logger.info(f"状态数据库已初始化: {self.db_path}")

    # ------------------------------------------------------------------
    # 构型管理
    # ------------------------------------------------------------------

    def load_configs(self, configs: dict[int, list[float]]):
        """
        从 Excel 读取的构型数据同步到数据库（断点续传：保留已有状态）。

        同步规则：
        1. 设计表中有但数据库中无 → 新建构型及步骤记录（状态 Waiting）
        2. 设计表中有且数据库中也有 → 更新参数，保留已有步骤状态
        3. 数据库中有但设计表中无 → 删除该构型及其所有步骤记录

        Args:
            configs: {构型名称: [参数1, 参数2, 参数3, 参数4]}
        """
        with self._lock:
            with self._get_connection() as conn:
                # 1) 获取数据库中已有的构型列表
                existing_rows = conn.execute(
                    "SELECT config_name FROM configs"
                ).fetchall()
                existing_configs = {row["config_name"] for row in existing_rows}

                new_configs = set(configs.keys())

                # 2) 删除设计表中已不存在的构型（含其所有步骤记录）
                removed_configs = existing_configs - new_configs
                if removed_configs:
                    for cn in removed_configs:
                        conn.execute(
                            "DELETE FROM remote_tasks WHERE config_name = ?",
                            (cn,)
                        )
                        conn.execute(
                            "DELETE FROM steps WHERE config_name = ?",
                            (cn,)
                        )
                        conn.execute(
                            "DELETE FROM configs WHERE config_name = ?",
                            (cn,)
                        )
                        logger.info(f"已从数据库移除已不存在的构型{cn}")

                # 3) 插入或更新构型参数；仅为新构型创建步骤记录
                #    使用 executemany 批量操作提升性能
                added_count = 0
                updated_count = 0
                new_step_rows = []
                config_rows = []
                for config_name, params in configs.items():
                    is_new = config_name not in existing_configs
                    config_rows.append((config_name, *params))
                    for step_name in STEP_NAMES:
                        new_step_rows.append(
                            (
                                config_name,
                                step_name,
                                STATUS_WAITING,
                                DEFAULT_WORKSTATION_ID,
                            )
                        )
                    if is_new:
                        added_count += 1
                        logger.debug(f"[State] 新增构型{config_name}: 参数 = {params}")
                    else:
                        updated_count += 1
                        logger.debug(f"[State] 更新构型{config_name}: 参数 = {params}")

                conn.executemany("""
                    INSERT INTO configs (config_name, param1, param2, param3, param4)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(config_name) DO UPDATE SET
                        param1 = excluded.param1,
                        param2 = excluded.param2,
                        param3 = excluded.param3,
                        param4 = excluded.param4
                """, config_rows)

                if new_step_rows:
                    conn.executemany("""
                        INSERT OR IGNORE INTO steps (
                            config_name, step_name, status, workstation_id
                        )
                        VALUES (?, ?, ?, ?)
                    """, new_step_rows)

        logger.info(
            f"已同步构型数据到状态库: 新增 {added_count}，更新 {updated_count}，"
            f"删除 {len(removed_configs) if removed_configs else 0}"
        )

    def get_all_configs(self) -> list[int]:
        """获取所有构型名称列表。"""
        with self._get_connection(readonly=True) as conn:
            rows = conn.execute("SELECT config_name FROM configs ORDER BY config_name").fetchall()
            return [row["config_name"] for row in rows]

    def get_config_params(self, config_name: int) -> list[float] | None:
        """获取指定构型的参数。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT param1, param2, param3, param4 FROM configs WHERE config_name = ?",
                (config_name,)
            ).fetchone()
            if row:
                return [row["param1"], row["param2"], row["param3"], row["param4"]]
            return None

    # ------------------------------------------------------------------
    # 步骤状态读写
    # ------------------------------------------------------------------

    def get_step_status(self, config_name: int, step_name: str) -> str:
        """获取指定构型指定步骤的状态。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT status FROM steps WHERE config_name = ? AND step_name = ?",
                (config_name, step_name)
            ).fetchone()
            result: str = row["status"] if row else STATUS_WAITING
            return result

    def get_step_status_counts(self, step_name: str) -> dict[str, int]:
        """Return status distribution for one step using one aggregate query."""
        with self._get_connection(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM steps
                WHERE step_name = ?
                GROUP BY status
                """,
                (step_name,),
            ).fetchall()
        return {str(row["status"]): int(row["cnt"]) for row in rows}

    def set_step_status(self, config_name: int, step_name: str, status: str,
                        error_message: str = ""):
        """
        设置指定构型指定步骤的状态。

        Args:
            config_name: 构型名称
            step_name: 步骤名 (sw/sc/transfer/meshing/solver)
            status: 状态值
            error_message: 错误信息（仅在 Error 状态时使用）
        """
        if status not in ALL_STATUSES:
            raise ValueError(f"无效状态: {status}，有效值: {ALL_STATUSES}")

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    UPDATE steps
                    SET status = ?, error_message = ?, updated_at = strftime('%s','now')
                    WHERE config_name = ? AND step_name = ?
                """, (status, error_message, config_name, step_name))
                if cursor.rowcount == 0:
                    config_exists = conn.execute(
                        "SELECT 1 FROM configs WHERE config_name = ?",
                        (config_name,),
                    ).fetchone()
                    if config_exists is None:
                        logger.warning(
                            "[State] 状态更新目标构型不存在: 构型%s [%s] -> %s",
                            config_name,
                            step_name,
                            status,
                        )
                    elif step_name in STEP_NAMES:
                        logger.warning(
                            "[State] 步骤记录缺失，已补齐后更新: 构型%s [%s] -> %s",
                            config_name,
                            step_name,
                            status,
                        )
                        conn.execute("""
                            INSERT INTO steps (
                                config_name, step_name, status, error_message, workstation_id
                            )
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            config_name,
                            step_name,
                            status,
                            error_message,
                            DEFAULT_WORKSTATION_ID,
                        ))
                    else:
                        logger.warning(
                            "[State] 状态更新目标步骤不存在: 构型%s [%s] -> %s",
                            config_name,
                            step_name,
                            status,
                        )
                logger.info(f"状态更新: 构型{config_name} [{step_name}] -> {status}")

    def get_all_steps_for_config(self, config_name: int) -> dict[str, dict]:
        """获取指定构型的所有步骤状态详情。"""
        with self._get_connection(readonly=True) as conn:
            rows = conn.execute(
                "SELECT step_name, status, retry_count, error_message FROM steps WHERE config_name = ?",
                (config_name,)
            ).fetchall()
            return {
                row["step_name"]: {
                    "status": row["status"],
                    "retry_count": row["retry_count"],
                    "error_message": row["error_message"],
                }
                for row in rows
            }

    def get_all_statuses(self) -> dict[int, dict[str, str]]:
        """
        获取所有构型所有步骤的状态（用于 TUI 渲染）。

        Returns:
            {config_name: {step_name: status, ...}, ...}
        """
        with self._get_connection(readonly=True) as conn:
            rows = conn.execute(
                "SELECT config_name, step_name, status FROM steps ORDER BY config_name, step_name"
            ).fetchall()

        result: dict[int, dict[str, str]] = {}
        for row in rows:
            cn = row["config_name"]
            if cn not in result:
                result[cn] = {}
            result[cn][row["step_name"]] = row["status"]
        return result

    def increment_retry(self, config_name: int, step_name: str) -> int:
        """增加重试计数并返回当前值。"""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE steps SET retry_count = retry_count + 1 WHERE config_name = ? AND step_name = ?",
                    (config_name, step_name)
                )
                if cursor.rowcount == 0:
                    logger.warning(
                        "[State] increment_retry: 构型%s 步骤%s 记录不存在",
                        config_name,
                        step_name,
                    )
                row = conn.execute(
                    "SELECT retry_count FROM steps WHERE config_name = ? AND step_name = ?",
                    (config_name, step_name)
                ).fetchone()
            result: int = row["retry_count"] if row else 0
            return result

    def get_step_retry_count(self, config_name: int, step_name: str) -> int:
        """查询指定构型指定步骤的当前重试次数。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT retry_count FROM steps WHERE config_name = ? AND step_name = ?",
                (config_name, step_name),
            ).fetchone()
            result: int = row["retry_count"] if row else 0
            return result

    # ------------------------------------------------------------------
    # 远程任务元数据
    # ------------------------------------------------------------------

    def save_remote_task(
        self,
        *,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
        config_name: int,
        step_name: str,
        task_name: str,
        flag_file: str,
        error_flag_file: str,
        log_file: str | None = None,
        pid_file: str | None = None,
        script_file: str | None = None,
        started_at: float,
    ) -> None:
        """保存或更新远程计划任务元数据。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO remote_tasks (
                        workstation_id, config_name, step_name, task_name, flag_file,
                        error_flag_file, log_file, pid_file, script_file,
                        started_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workstation_id, config_name, step_name) DO UPDATE SET
                        task_name = excluded.task_name,
                        flag_file = excluded.flag_file,
                        error_flag_file = excluded.error_flag_file,
                        log_file = excluded.log_file,
                        pid_file = excluded.pid_file,
                        script_file = excluded.script_file,
                        started_at = excluded.started_at
                    """,
                    (
                        workstation_id,
                        config_name,
                        step_name,
                        task_name,
                        flag_file,
                        error_flag_file,
                        log_file,
                        pid_file,
                        script_file,
                        started_at,
                    ),
                )

    def get_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, object] | None:
        """获取指定构型和步骤的远程任务元数据。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                """
                SELECT workstation_id, config_name, step_name, task_name, flag_file,
                       error_flag_file, log_file, pid_file, script_file,
                       started_at
                FROM remote_tasks
                WHERE workstation_id = ? AND config_name = ? AND step_name = ?
                """,
                (workstation_id, config_name, step_name),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_all_remote_tasks(
        self,
        workstation_id: str | None = None,
    ) -> list[dict[str, object]]:
        """列出所有持久化的远程任务元数据。"""
        with self._get_connection(readonly=True) as conn:
            if workstation_id is None:
                rows = conn.execute(
                    """
                    SELECT workstation_id, config_name, step_name, task_name, flag_file,
                           error_flag_file, log_file, pid_file, script_file,
                           started_at
                    FROM remote_tasks
                    ORDER BY workstation_id, config_name, step_name
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT workstation_id, config_name, step_name, task_name, flag_file,
                           error_flag_file, log_file, pid_file, script_file,
                           started_at
                    FROM remote_tasks
                    WHERE workstation_id = ?
                    ORDER BY config_name, step_name
                    """,
                    (workstation_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """删除指定构型和步骤的远程任务元数据。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    DELETE FROM remote_tasks
                    WHERE workstation_id = ? AND config_name = ? AND step_name = ?
                    """,
                    (workstation_id, config_name, step_name),
                )

    def delete_remote_tasks_for_config(
        self,
        config_name: int,
        from_step: str | None = None,
    ) -> None:
        """删除构型在指定步骤及其下游的远程任务元数据。"""
        if from_step is None:
            steps_to_delete = STEP_NAMES
        else:
            start_idx = STEP_INDEX.get(from_step, 0)
            steps_to_delete = STEP_NAMES[start_idx:]
        with self._lock:
            with self._get_connection() as conn:
                conn.executemany(
                    "DELETE FROM remote_tasks WHERE config_name = ? AND step_name = ?",
                    [(config_name, step_name) for step_name in steps_to_delete],
                )

    def delete_all_remote_tasks(self, workstation_id: str | None = None) -> None:
        """删除所有或指定工作站的远程任务元数据。"""
        with self._lock:
            with self._get_connection() as conn:
                if workstation_id is None:
                    conn.execute("DELETE FROM remote_tasks")
                else:
                    conn.execute(
                        "DELETE FROM remote_tasks WHERE workstation_id = ?",
                        (workstation_id,),
                    )

    def set_config_workstation(
        self,
        config_name: int,
        workstation_id: str,
        slot_id: int | None = None,
    ) -> None:
        """保存构型到工作站的分配关系。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE steps
                    SET workstation_id = ?, slot_id = ?
                    WHERE config_name = ?
                    """,
                    (workstation_id, slot_id, config_name),
                )

    def get_config_workstation(self, config_name: int) -> str | None:
        """读取构型的工作站分配关系。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                """
                SELECT workstation_id
                FROM steps
                WHERE config_name = ? AND workstation_id IS NOT NULL
                ORDER BY step_name
                LIMIT 1
                """,
                (config_name,),
            ).fetchone()
        return str(row["workstation_id"]) if row else None

    def set_meshing_running_if_idle(
        self,
        config_name: int,
        workstation_id: str | None = None,
    ) -> bool:
        """原子设置 Meshing 为 Running，同一时刻只允许一个构型执行网格划分。

        在同一个 self._lock 临界区内完成 SELECT + UPDATE，
        确保同一工作站不会有多个构型同时处于 Meshing Running 状态。

        Args:
            config_name: 要启动网格划分的构型名称
            workstation_id: 工作站 ID；None 保持旧的全局单工作站语义

        Returns:
            True 表示成功设置为 Running；False 表示已有其他构型在执行
        """
        with self._lock:
            with self._get_connection() as conn:
                # De-dup SELECT: unified query with optional workstation filter
                params: list[object] = [STATUS_RUNNING, STATUS_RETRYING]
                ws_filter = ""
                if workstation_id is not None:
                    ws_filter = " AND workstation_id = ?"
                    params.append(workstation_id)
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM steps "
                    "WHERE step_name = 'meshing' AND status IN (?, ?)"
                    + ws_filter,
                    params,
                ).fetchone()
                if (row["cnt"] or 0) > 0:
                    logger.debug(
                        f"set_meshing_running_if_idle({config_name}): "
                        f"已有其他构型在执行网格划分，拒绝"
                    )
                    return False
                # De-dup UPDATE: unified statement with optional workstation_id
                update_params: list[object] = [STATUS_RUNNING, config_name, "meshing"]
                ws_set = ""
                if workstation_id is not None:
                    ws_set = " workstation_id = ?,"
                    update_params.insert(1, workstation_id)
                conn.execute(
                    "UPDATE steps SET status = ?, error_message = '',"
                    + ws_set
                    + " updated_at = strftime('%s','now') "
                    "WHERE config_name = ? AND step_name = ?",
                    update_params,
                )
                logger.info(f"状态更新: 构型{config_name} [Meshing] -> Running（原子防护通过）")
                return True

    # ------------------------------------------------------------------
    # 批量状态操作（用于 reset 命令）
    # ------------------------------------------------------------------

    def reset_config_steps(self, config_name: int | str, from_step: str | None = None) -> None:
        """
        重置指定构型的步骤状态。

        Args:
            config_name: 构型名称 (int) 或 "all" 表示全部构型
            from_step: 从此步骤开始重置（包含此步骤），若为 None 则重置所有步骤
        """
        if config_name == "all":
            for cn in self.get_all_configs():
                self._reset_single_config(cn, from_step)
        else:
            self._reset_single_config(int(config_name), from_step)

    def _reset_single_config(self, config_name: int, from_step: str | None = None):
        """重置单个构型的步骤状态（内部方法）。"""
        effective_from_step = "transfer" if from_step == "meshing" else from_step
        start_idx = STEP_INDEX.get(effective_from_step, 0) if effective_from_step else 0
        steps_to_reset = STEP_NAMES[start_idx:]

        with self._lock:
            with self._get_connection() as conn:
                for step_name in steps_to_reset:
                    conn.execute(
                        "DELETE FROM remote_tasks WHERE config_name = ? AND step_name = ?",
                        (config_name, step_name),
                    )
                for step_name in steps_to_reset:
                    conn.execute("""
                        UPDATE steps
                        SET status = ?, retry_count = 0, error_message = '', updated_at = strftime('%s','now')
                        WHERE config_name = ? AND step_name = ?
                    """, (STATUS_WAITING, config_name, step_name))
                if from_step is None or STEP_INDEX.get(from_step, 0) <= STEP_INDEX["meshing"]:
                    conn.execute(
                        """
                        UPDATE steps
                        SET workstation_id = ?, slot_id = NULL, updated_at = strftime('%s','now')
                        WHERE config_name = ?
                        """,
                        (DEFAULT_WORKSTATION_ID, config_name),
                    )
                # 如果重置了 SW，需谨慎处理 sw_macro_started 标志：
                # 仅当数据库中不再有任何 SW=Completed 的构型时才清除该标志。
                # 这样可以避免部分重置（仅重置单个构型）时意外允许全部重跑 SW。
                if from_step == "sw" or from_step is None:
                    remaining = conn.execute(
                        "SELECT COUNT(*) as cnt FROM steps "
                        "WHERE step_name = 'sw' AND status = ?",
                        (STATUS_COMPLETED,)
                    ).fetchone()
                    if not remaining or remaining["cnt"] == 0:
                        conn.execute(
                            "UPDATE engine_state SET value = ? WHERE key = ?",
                            ("false", "sw_macro_started")
                        )
                    else:
                        logger.debug(
                            f"仍有 {remaining['cnt'] if remaining else 0} 个构型的 SW=Completed，保持 sw_macro_started=true"
                        )
                if from_step is None or STEP_INDEX.get(from_step, 0) <= STEP_INDEX["solver"]:
                    conn.execute(
                        "DELETE FROM engine_state WHERE key = ?",
                        ("solver_progress",),
                    )
                    self._clear_solver_progress_by_config_locked(conn, config_name)

        logger.info(f"已重置构型 {config_name} 从 {from_step or 'sw'} 起的所有步骤")

    def reset_all(self):
        """重置所有构型的所有步骤（含引擎全局状态）。"""
        for cn in self.get_all_configs():
            self._reset_single_config(cn, None)
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE engine_state SET value = ? WHERE key = ?",
                    ("false", "global_barrier_met"),
                )
                conn.execute(
                    "UPDATE engine_state SET value = ? WHERE key = ?",
                    ("0", "error_count"),
                )
                conn.execute(
                    "DELETE FROM engine_state WHERE key = ?",
                    ("solver_progress",),
                )
                conn.execute(
                    "DELETE FROM engine_state WHERE key = ?",
                    ("solver_progress_by_config",),
                )
        logger.warning("已重置所有构型的所有步骤！")

    # ------------------------------------------------------------------
    # 引擎全局状态
    # ------------------------------------------------------------------

    def get_engine_status(self) -> str:
        """获取引擎状态：stopped | running | paused。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'engine_status'"
            ).fetchone()
            result: str = row["value"] if row else "stopped"
            return result

    def set_engine_status(self, status: str):
        """设置引擎状态。"""
        valid = ["stopped", "running", "paused"]
        if status not in valid:
            raise ValueError(f"无效引擎状态: {status}")
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE engine_state SET value = ? WHERE key = 'engine_status'",
                    (status,)
                )
        logger.info(f"引擎状态变更: -> {status}")

    def set_solver_quarantine(
        self,
        quarantine: dict[str, dict[str, int | float]],
    ) -> None:
        """持久化 Solver 工作站隔离状态。"""
        with self._lock:
            with self._get_connection() as conn:
                if quarantine:
                    conn.execute(
                        "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
                        ("solver_quarantine", json.dumps(quarantine, ensure_ascii=False)),
                    )
                else:
                    conn.execute("DELETE FROM engine_state WHERE key = ?", ("solver_quarantine",))

    def get_solver_quarantine(self) -> dict[str, dict[str, int | float]]:
        """读取 Solver 工作站隔离状态；不存在或损坏时返回空字典。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = ?",
                ("solver_quarantine",),
            ).fetchone()
        if not row or not row["value"]:
            return {}
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            logger.warning("[State] solver_quarantine JSON 损坏，已忽略")
            return {}
        if not isinstance(value, dict):
            return {}
        restored: dict[str, dict[str, int | float]] = {}
        for workstation_id, data in value.items():
            if not isinstance(workstation_id, str) or not isinstance(data, dict):
                continue
            failure_count = data.get("failure_count")
            until = data.get("until")
            if not isinstance(failure_count, int | float) or not isinstance(until, int | float):
                continue
            restored[workstation_id] = {
                "failure_count": int(failure_count),
                "until": float(until),
            }
        return restored

    def set_solver_progress(self, progress: dict[str, object]) -> None:
        """存储当前 Solver 剩余时间进度。"""
        payload = json.dumps(progress, ensure_ascii=False)
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
                    ("solver_progress", payload),
                )
                config_name = progress.get("config_name")
                if config_name is not None:
                    try:
                        if not isinstance(config_name, str | int | float):
                            raise TypeError
                        self._set_solver_progress_by_config_locked(
                            conn,
                            int(config_name),
                            progress,
                        )
                    except (TypeError, ValueError):
                        logger.debug("[State] solver_progress 缺少有效 config_name，跳过 per-config map")

    def get_solver_progress(self) -> dict[str, object] | None:
        """读取当前 Solver 剩余时间进度；不存在或损坏时返回 None。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = ?",
                ("solver_progress",),
            ).fetchone()
        if not row or not row["value"]:
            return None
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            logger.warning("[State] solver_progress JSON 损坏，已忽略")
        else:
            if isinstance(value, dict):
                return value
        progress_by_config = self.get_solver_progress_by_config()
        return self._latest_solver_progress_from_map(progress_by_config)

    def set_solver_progress_by_config(
        self,
        config_name: int,
        progress: dict[str, object],
    ) -> None:
        """按构型存储 Solver 剩余时间进度。"""
        with self._lock:
            with self._get_connection() as conn:
                self._set_solver_progress_by_config_locked(conn, config_name, progress)
                conn.execute(
                    "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
                    ("solver_progress", json.dumps(progress, ensure_ascii=False)),
                )

    def get_solver_progress_by_config(self) -> dict[str, dict[str, object]]:
        """读取所有构型的 Solver 剩余时间进度。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = ?",
                ("solver_progress_by_config",),
            ).fetchone()
        if not row or not row["value"]:
            return {}
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            logger.warning("[State] solver_progress_by_config JSON 损坏，已忽略")
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(key): progress
            for key, progress in value.items()
            if isinstance(progress, dict)
        }

    def clear_solver_progress_by_config(self, config_name: int) -> None:
        """清理指定构型的 Solver progress。"""
        with self._lock:
            with self._get_connection() as conn:
                self._clear_solver_progress_by_config_locked(conn, config_name)

    def clear_solver_progress(self) -> None:
        """清理当前 Solver 剩余时间进度。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "DELETE FROM engine_state WHERE key = ?",
                    ("solver_progress",),
                )
                conn.execute(
                    "DELETE FROM engine_state WHERE key = ?",
                    ("solver_progress_by_config",),
                )

    def _set_solver_progress_by_config_locked(
        self,
        conn: sqlite3.Connection,
        config_name: int,
        progress: dict[str, object],
    ) -> None:
        progress_by_config = self._read_solver_progress_by_config_locked(conn)
        progress_by_config[str(config_name)] = progress
        conn.execute(
            "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
            ("solver_progress_by_config", json.dumps(progress_by_config, ensure_ascii=False)),
        )

    def _clear_solver_progress_by_config_locked(
        self,
        conn: sqlite3.Connection,
        config_name: int,
    ) -> None:
        progress_by_config = self._read_solver_progress_by_config_locked(conn)
        progress_by_config.pop(str(config_name), None)
        if progress_by_config:
            conn.execute(
                "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
                ("solver_progress_by_config", json.dumps(progress_by_config, ensure_ascii=False)),
            )
            latest_progress = self._latest_solver_progress_from_map(progress_by_config)
            if latest_progress is None:
                conn.execute("DELETE FROM engine_state WHERE key = ?", ("solver_progress",))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO engine_state (key, value) VALUES (?, ?)",
                    (
                        "solver_progress",
                        json.dumps(latest_progress, ensure_ascii=False),
                    ),
                )
        else:
            conn.execute("DELETE FROM engine_state WHERE key = ?", ("solver_progress_by_config",))
            conn.execute("DELETE FROM engine_state WHERE key = ?", ("solver_progress",))

    @staticmethod
    def _read_solver_progress_by_config_locked(
        conn: sqlite3.Connection,
    ) -> dict[str, dict[str, object]]:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = ?",
            ("solver_progress_by_config",),
        ).fetchone()
        if not row or not row["value"]:
            return {}
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(key): progress
            for key, progress in value.items()
            if isinstance(progress, dict)
        }

    @staticmethod
    def _latest_solver_progress_from_map(
        progress_by_config: dict[str, dict[str, object]],
    ) -> dict[str, object] | None:
        for key in reversed(progress_by_config):
            progress = progress_by_config.get(key)
            if isinstance(progress, dict):
                return progress
        return None

    def set_all_running_to_paused(self):
        """将所有 Running、Retrying 和 UnknownRemote 状态的步骤批量切换为 Paused。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE steps SET status = ?, updated_at = strftime('%s','now') "
                    "WHERE status IN (?, ?, ?)",
                    (STATUS_PAUSED, STATUS_RUNNING, STATUS_RETRYING, STATUS_UNKNOWN_REMOTE)
                )
        logger.info("已将所有运行中/重试中/远程未知步骤切换为 Paused")

    def is_sw_macro_started(self) -> bool:
        """检查 SW 宏是否已启动。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'sw_macro_started'"
            ).fetchone()
            return row["value"] == "true" if row else False

    def set_sw_macro_started(self, started: bool = True):
        """设置 SW 宏启动标志。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE engine_state SET value = ? WHERE key = 'sw_macro_started'",
                    ("true" if started else "false",)
                )

    def is_global_barrier_met(self) -> bool:
        """检查全局屏障是否已通过。"""
        with self._get_connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'global_barrier_met'"
            ).fetchone()
            return row["value"] == "true" if row else False

    def set_global_barrier_met(self, met: bool = True):
        """设置全局屏障通过标志。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE engine_state SET value = ? WHERE key = 'global_barrier_met'",
                    ("true" if met else "false",)
                )

    # ------------------------------------------------------------------
    # 辅助查询方法
    # ------------------------------------------------------------------

    def get_configs_at_step(self, step_name: str, status: str | None = None) -> list[int]:
        """获取处于指定步骤指定状态的构型列表。"""
        with self._get_connection(readonly=True) as conn:
            if status:
                rows = conn.execute(
                    "SELECT config_name FROM steps WHERE step_name = ? AND status = ? ORDER BY config_name",
                    (step_name, status)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT config_name FROM steps WHERE step_name = ? ORDER BY config_name",
                    (step_name,)
                ).fetchall()
            return [row["config_name"] for row in rows]

    def all_configs_completed_at_step(
        self,
        step_name: str,
        workstation_id: str | None = None,
        config_names: list[int] | None = None,
    ) -> bool:
        """
        检查所有构型在指定步骤是否全部为 Completed 状态。

        用于全局屏障判断：当所有构型的 Meshing 都 Completed 时，
        才能解锁 Solver。
        """
        with self._get_connection(readonly=True) as conn:
            clauses = ["step_name = ?", "status != ?"]
            params: list[object] = [step_name, STATUS_COMPLETED]
            if workstation_id is not None:
                clauses.append("COALESCE(workstation_id, ?) = ?")
                params.extend([DEFAULT_WORKSTATION_ID, workstation_id])
            if config_names is not None:
                if not config_names:
                    return True
                placeholders = ", ".join("?" for _ in config_names)
                clauses.append(f"config_name IN ({placeholders})")
                params.extend(config_names)
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM steps WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
            return (row["cnt"] or 0) == 0

    def get_error_configs(self) -> list[tuple[int, str, str]]:
        """获取所有处于 Error 状态的构型和步骤。"""
        with self._get_connection(readonly=True) as conn:
            rows = conn.execute(
                "SELECT config_name, step_name, error_message FROM steps WHERE status = ?",
                (STATUS_ERROR,)
            ).fetchall()
            return [(row["config_name"], row["step_name"], row["error_message"]) for row in rows]

    def get_statistics(self) -> dict:
        """
        获取全局统计信息。

        Returns:
            dict: {
                "total_configs": int,
                "steps": {step_name: {status: count, ...}, ...},
                "error_count": int,
            }
        """
        stats: dict = {
            "total_configs": 0,
            "steps": {},
            "error_count": 0,
        }

        # 预填充所有步骤×状态组合为 0（确保未出现的组合也有默认值）
        for step_name in STEP_NAMES:
            stats["steps"][step_name] = {status: 0 for status in ALL_STATUSES}

        with self._get_connection(readonly=True) as conn:
            # 总构型数
            row = conn.execute("SELECT COUNT(*) as cnt FROM configs").fetchone()
            stats["total_configs"] = row["cnt"] if row else 0

            # 单条 GROUP BY 查询获取所有步骤×状态的计数（替代原先的 N×M 次查询）
            rows = conn.execute(
                "SELECT step_name, status, COUNT(*) as cnt "
                "FROM steps GROUP BY step_name, status"
            ).fetchall()
            for row in rows:
                step_name = row["step_name"]
                status = row["status"]
                if step_name in stats["steps"]:
                    stats["steps"][step_name][status] = row["cnt"]

            # 错误总数（从已查询的数据中聚合，避免额外查询）
            stats["error_count"] = sum(
                stats["steps"][sn].get(STATUS_ERROR, 0) for sn in STEP_NAMES
            )

        return stats
