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
import sqlite3
import threading
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

from engine.config import (
    STEP_NAMES, STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR,
    ALL_STATUSES, IPC_CONFIG,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


class StateManager:
    """
    共享状态管理器。

    使用 SQLite WAL 模式，允许多个进程同时读取（TUI 客户端），
    写入操作由 Daemon 进程独占（通过 Python 线程锁保护）。
    """

    def __init__(self, db_path: str = None):
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
        """配置数据库 PRAGMA 设置（仅初始化一次）。"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式：读写并发
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")    # 启用外键约束
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    @contextmanager
    def _get_connection(self):
        """获取数据库连接（上下文管理器，自动提交/关闭）。
        
        注：PRAGMA 设置在 __init__ 中一次颒配置，此处不重复设置。
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception as e:
                logger.error(f"数据库回滚异常: {e}")
            raise
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.error(f"数据库连接关闭异常: {e}")

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
                    UNIQUE(config_name, step_name),
                    FOREIGN KEY(config_name) REFERENCES configs(config_name)
                )
            """)

            # 引擎全局状态表（单行记录）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engine_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

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

        logger.info(f"状态数据库已初始化: {self.db_path}")

    # ------------------------------------------------------------------
    # 构型管理
    # ------------------------------------------------------------------

    def load_configs(self, configs: Dict[int, List[float]]):
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
                            "DELETE FROM steps WHERE config_name = ?",
                            (cn,)
                        )
                        conn.execute(
                            "DELETE FROM configs WHERE config_name = ?",
                            (cn,)
                        )
                    logger.info(
                        f"已从数据库移除 {len(removed_configs)} 个已不存在的构型: "
                        f"{sorted(removed_configs)}"
                    )

                # 3) 插入或更新构型参数；仅为新构型创建步骤记录
                added_count = 0
                updated_count = 0
                for config_name, params in configs.items():
                    is_new = config_name not in existing_configs

                    conn.execute("""
                        INSERT OR REPLACE INTO configs (config_name, param1, param2, param3, param4)
                        VALUES (?, ?, ?, ?, ?)
                    """, (config_name, *params))

                    if is_new:
                        for step_name in STEP_NAMES:
                            conn.execute("""
                                INSERT OR IGNORE INTO steps (config_name, step_name, status)
                                VALUES (?, ?, ?)
                            """, (config_name, step_name, STATUS_WAITING))
                        added_count += 1
                    else:
                        updated_count += 1

        logger.info(
            f"已同步构型数据到状态库: 新增 {added_count}，更新 {updated_count}，"
            f"删除 {len(removed_configs) if removed_configs else 0}"
        )

    def get_all_configs(self) -> List[int]:
        """获取所有构型名称列表。"""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT config_name FROM configs ORDER BY config_name").fetchall()
            return [row["config_name"] for row in rows]

    def get_config_params(self, config_name: int) -> Optional[List[float]]:
        """获取指定构型的参数。"""
        with self._get_connection() as conn:
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
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM steps WHERE config_name = ? AND step_name = ?",
                (config_name, step_name)
            ).fetchone()
            return row["status"] if row else STATUS_WAITING

    def set_step_status(self, config_name: int, step_name: str, status: str,
                        error_message: str = ""):
        """
        设置指定构型指定步骤的状态。

        Args:
            config_name: 构型名称
            step_name: 步骤名 (SW/SC/Transfer/Meshing/Solver)
            status: 状态值
            error_message: 错误信息（仅在 Error 状态时使用）
        """
        if status not in ALL_STATUSES:
            raise ValueError(f"无效状态: {status}，有效值: {ALL_STATUSES}")

        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE steps
                    SET status = ?, error_message = ?, updated_at = strftime('%s','now')
                    WHERE config_name = ? AND step_name = ?
                """, (status, error_message, config_name, step_name))
                logger.info(f"状态更新: 构型{config_name} [{step_name}] -> {status}")

    def get_all_steps_for_config(self, config_name: int) -> Dict[str, dict]:
        """获取指定构型的所有步骤状态详情。"""
        with self._get_connection() as conn:
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

    def get_all_statuses(self) -> Dict[int, Dict[str, str]]:
        """
        获取所有构型所有步骤的状态（用于 TUI 渲染）。

        Returns:
            {config_name: {step_name: status, ...}, ...}
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT config_name, step_name, status FROM steps ORDER BY config_name, step_name"
            ).fetchall()

        result: Dict[int, Dict[str, str]] = {}
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
                conn.execute(
                    "UPDATE steps SET retry_count = retry_count + 1 WHERE config_name = ? AND step_name = ?",
                    (config_name, step_name)
                )
                row = conn.execute(
                    "SELECT retry_count FROM steps WHERE config_name = ? AND step_name = ?",
                    (config_name, step_name)
                ).fetchone()
                return row["retry_count"] if row else 0

    # ------------------------------------------------------------------
    # 批量状态操作（用于 reset 命令）
    # ------------------------------------------------------------------

    def reset_config_steps(self, config_name, from_step: str = None):
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
            self._reset_single_config(config_name, from_step)

    def _reset_single_config(self, config_name: int, from_step: str = None):
        """重置单个构型的步骤状态（内部方法）。"""
        start_idx = STEP_INDEX.get(from_step, 0) if from_step else 0
        steps_to_reset = STEP_NAMES[start_idx:]

        with self._lock:
            with self._get_connection() as conn:
                for step_name in steps_to_reset:
                    conn.execute("""
                        UPDATE steps
                        SET status = ?, retry_count = 0, error_message = '', updated_at = strftime('%s','now')
                        WHERE config_name = ? AND step_name = ?
                    """, (STATUS_WAITING, config_name, step_name))
                # 如果重置了 SW，需谨慎处理 sw_macro_started 标志：
                # 仅当数据库中不再有任何 SW=Completed 的构型时才清除该标志。
                # 这样可以避免部分重置（仅重置单个构型）时意外允许全部重跑 SW。
                if from_step == "SW" or from_step is None:
                    remaining = conn.execute(
                        "SELECT COUNT(*) as cnt FROM steps "
                        "WHERE step_name = 'SW' AND status = ?",
                        (STATUS_COMPLETED,)
                    ).fetchone()
                    if not remaining or remaining["cnt"] == 0:
                        conn.execute(
                            "UPDATE engine_state SET value = ? WHERE key = ?",
                            ("false", "sw_macro_started")
                        )
                    else:
                        logger.debug(
                            "仍有 %d 个构型的 SW=Completed，保持 sw_macro_started=true",
                            remaining["cnt"] if remaining else 0,
                        )

        logger.info(f"已重置构型 {config_name} 从 {from_step or 'SW'} 起的所有步骤")

    def reset_all(self):
        """重置所有构型的所有步骤（含引擎全局状态）。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("UPDATE steps SET status = ?, retry_count = 0, error_message = ?",
                           (STATUS_WAITING, ""))
                conn.execute("UPDATE engine_state SET value = ? WHERE key = ?",
                           ("false", "sw_macro_started"))
                conn.execute("UPDATE engine_state SET value = ? WHERE key = ?",
                           ("false", "global_barrier_met"))
                conn.execute("UPDATE engine_state SET value = ? WHERE key = ?",
                           ("0", "error_count"))
        logger.warning("已重置所有构型的所有步骤！")

    # ------------------------------------------------------------------
    # 引擎全局状态
    # ------------------------------------------------------------------

    def get_engine_status(self) -> str:
        """获取引擎状态：stopped | running | paused。"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'engine_status'"
            ).fetchone()
            return row["value"] if row else "stopped"

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

    def set_all_running_to_paused(self):
        """将所有 Running 和 Retrying 状态的步骤批量切换为 Paused。"""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE steps SET status = ?, updated_at = strftime('%s','now') "
                    "WHERE status IN (?, ?)",
                    (STATUS_PAUSED, STATUS_RUNNING, STATUS_RETRYING)
                )
        logger.info("已将所有运行中/重试中步骤切换为 Paused")

    def set_all_paused_to_running(self, exclude_steps: list[str] | None = None):
        """将所有 Paused 状态的步骤恢复为 Running。

        Args:
            exclude_steps: 可选的步骤名列表，这些步骤的 Paused 状态不会被修改
        """
        with self._lock:
            with self._get_connection() as conn:
                if exclude_steps:
                    placeholders = ','.join(['?'] * len(exclude_steps))
                    conn.execute(
                        f"UPDATE steps SET status = ?, updated_at = strftime('%s','now') "
                        f"WHERE status = ? AND step_name NOT IN ({placeholders})",
                        (STATUS_RUNNING, STATUS_PAUSED, *exclude_steps)
                    )
                else:
                    conn.execute(
                        "UPDATE steps SET status = ?, updated_at = strftime('%s','now') "
                        "WHERE status = ?",
                        (STATUS_RUNNING, STATUS_PAUSED)
                    )
        if exclude_steps:
            logger.info(f"已将所有 Paused 步骤恢复为 Running（排除步骤: {exclude_steps}）")
        else:
            logger.info("已将所有 Paused 步骤恢复为 Running")

    def is_sw_macro_started(self) -> bool:
        """检查 SW 宏是否已启动。"""
        with self._get_connection() as conn:
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
        with self._get_connection() as conn:
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

    def get_configs_at_step(self, step_name: str, status: str = None) -> List[int]:
        """获取处于指定步骤指定状态的构型列表。"""
        with self._get_connection() as conn:
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

    def all_configs_completed_at_step(self, step_name: str) -> bool:
        """
        检查所有构型在指定步骤是否全部为 Completed 状态。

        用于全局屏障判断：当所有构型的 Meshing 都 Completed 时，
        才能解锁 Solver。
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM steps WHERE step_name = ? AND status != ?",
                (step_name, STATUS_COMPLETED)
            ).fetchone()
            return (row["cnt"] or 0) == 0

    def get_error_configs(self) -> List[Tuple[int, str, str]]:
        """获取所有处于 Error 状态的构型和步骤。"""
        with self._get_connection() as conn:
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

        with self._get_connection() as conn:
            # 总构型数
            row = conn.execute("SELECT COUNT(*) as cnt FROM configs").fetchone()
            stats["total_configs"] = row["cnt"] if row else 0

            # 每个步骤的状态分布
            for step_name in STEP_NAMES:
                step_counts: dict = {}
                for status in ALL_STATUSES:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM steps WHERE step_name = ? AND status = ?",
                        (step_name, status)
                    ).fetchone()
                    step_counts[status] = row["cnt"] if row else 0
                stats["steps"][step_name] = step_counts

            # 错误总数
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM steps WHERE status = ?",
                (STATUS_ERROR,)
            ).fetchone()
            stats["error_count"] = row["cnt"] if row else 0

        return stats
