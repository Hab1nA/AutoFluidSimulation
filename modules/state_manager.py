# =============================================================================
# state_manager.py — SQLite 状态管理与断点续传逻辑（阶段1）
# 
# 功能：
#   1. 维护基于 SQLite 的任务状态数据库
#   2. 提供线程安全的状态读写操作
#   3. 启动时自动检测并恢复未完成的任务
#   4. 支持重试计数与错误信息记录
# =============================================================================
import sqlite3
import threading
import time
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, List, Tuple

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态常量定义
# ---------------------------------------------------------------------------
class Status:
    """所有可能的任务阶段状态"""
    PENDING     = "Pending"       # 尚未开始
    IN_PROGRESS = "InProgress"    # 正在执行中
    DONE        = "Done"          # 已完成
    ERROR       = "Error"         # 发生错误
    TRANSFERRED = "Transferred"   # 文件已传输到远程
    COMPUTING   = "Computing"     # 远程正在计算
    COMPLETED   = "Completed"     # 全部完成（Fluent 求解结束）


class StateManager:
    """
    基于 SQLite 的流水线任务状态管理器。
    提供线程安全的 CRUD 操作，支持断点续传。
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化数据库连接。

        Args:
            db_path: SQLite 数据库文件路径，默认使用 config 中的配置
        """
        self.db_path = db_path or config.LOCAL_CONFIG["db_path"]
        self._lock = threading.Lock()  # 保护所有写操作的线程锁
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    # -----------------------------------------------------------------------
    # 数据库初始化
    # -----------------------------------------------------------------------
    def _get_connection(self) -> sqlite3.Connection:
        """获取或创建数据库连接（每个线程独立连接）"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # 启用 WAL 模式以提高并发性能
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
        return self._conn

    def _init_database(self):
        """创建数据库表结构（如果尚未存在）"""
        conn = self._get_connection()
        with self._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_state (
                    config_id       TEXT PRIMARY KEY NOT NULL,
                    sw_status       TEXT NOT NULL DEFAULT 'Pending',
                    sc_status       TEXT NOT NULL DEFAULT 'Pending',
                    transfer_status TEXT NOT NULL DEFAULT 'Pending',
                    fluent_status   TEXT NOT NULL DEFAULT 'Pending',
                    retry_count     INTEGER NOT NULL DEFAULT 0,
                    error_msg       TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
            """)
            conn.commit()
            logger.info("数据库初始化完成: %s", self.db_path)

        # 自动初始化所有参数组合的任务记录（如果尚不存在）
        self._ensure_all_configs_exist()

    def _ensure_all_configs_exist(self):
        """确保 config.PARAMETER_SETS 中的所有构型在数据库中有对应记录"""
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for param_set in config.PARAMETER_SETS:
                cid = param_set["config_id"]
                existing = conn.execute(
                    "SELECT 1 FROM task_state WHERE config_id = ?", (cid,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO task_state (config_id, created_at, updated_at) VALUES (?, ?, ?)",
                        (cid, now, now),
                    )
            conn.commit()

    # -----------------------------------------------------------------------
    # 通用状态读写
    # -----------------------------------------------------------------------
    def get_state(self, config_id: str) -> Optional[Dict]:
        """
        获取指定构型的完整状态记录。

        Returns:
            包含所有字段的字典，若不存在则返回 None
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM task_state WHERE config_id = ?", (config_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_all_states(self) -> List[Dict]:
        """获取所有构型的状态列表"""
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM task_state ORDER BY config_id").fetchall()
        return [dict(r) for r in rows]

    def update_status(
        self,
        config_id: str,
        sw_status: Optional[str] = None,
        sc_status: Optional[str] = None,
        transfer_status: Optional[str] = None,
        fluent_status: Optional[str] = None,
        error_msg: Optional[str] = None,
        increment_retry: bool = False,
    ):
        """
        更新指定构型的状态字段。只更新传入的非 None 字段。

        Args:
            config_id: 构型 ID
            sw_status / sc_status / transfer_status / fluent_status: 阶段状态值
            error_msg: 错误信息（None 表示不更新）
            increment_retry: 是否将 retry_count +1
        """
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fields = []
            values = []

            if sw_status is not None:
                fields.append("sw_status = ?")
                values.append(sw_status)
            if sc_status is not None:
                fields.append("sc_status = ?")
                values.append(sc_status)
            if transfer_status is not None:
                fields.append("transfer_status = ?")
                values.append(transfer_status)
            if fluent_status is not None:
                fields.append("fluent_status = ?")
                values.append(fluent_status)
            if error_msg is not None:
                fields.append("error_msg = ?")
                values.append(error_msg)

            fields.append("updated_at = ?")
            values.append(now)

            if increment_retry:
                fields.append("retry_count = retry_count + 1")

            values.append(config_id)
            sql = f"UPDATE task_state SET {', '.join(fields)} WHERE config_id = ?"
            conn.execute(sql, values)
            conn.commit()

    def reset_config(self, config_id: str):
        """将指定构型的所有状态重置为 Pending，重试计数清零（用于手动重跑）"""
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE task_state 
                   SET sw_status='Pending', sc_status='Pending', 
                       transfer_status='Pending', fluent_status='Pending',
                       retry_count=0, error_msg=NULL, updated_at=?
                   WHERE config_id = ?""",
                (now, config_id),
            )
            conn.commit()

    def is_config_complete(self, config_id: str) -> bool:
        """
        判断一个构型是否已全部完成（所有阶段均为 Done/Completed）。
        """
        state = self.get_state(config_id)
        if state is None:
            return False
        return (
            state["sw_status"] == Status.DONE
            and state["sc_status"] == Status.DONE
            and state["transfer_status"] == Status.DONE
            and state["fluent_status"] == Status.COMPLETED
        )

    def get_next_pending_stage(self, config_id: str) -> Optional[str]:
        """
        返回指定构型下一个待执行的阶段名称。
        完全按照流水线顺序判断：SW → SC → Transfer → Fluent/Solver

        Returns:
            阶段名称 ('sw', 'sc', 'transfer', 'fluent') 或 None（全部完成）
        """
        state = self.get_state(config_id)
        if state is None:
            return "sw"

        if state["sw_status"] != Status.DONE:
            return "sw"
        if state["sc_status"] != Status.DONE:
            return "sc"
        if state["transfer_status"] != Status.DONE:
            return "transfer"
        if state["fluent_status"] not in (Status.COMPLETED, Status.COMPUTING):
            return "fluent"
        return None

    def get_stats(self) -> Dict:
        """
        获取全局统计信息，用于 TUI 显示。

        Returns:
            {
                'total': 总数,
                'completed': 全部完成数,
                'in_progress': 进行中数,
                'error': 出错数,
                'pending': 等待中数,
            }
        """
        rows = self.get_all_states()
        total = len(rows)
        completed = sum(
            1 for r in rows
            if r["sw_status"] == Status.DONE
            and r["sc_status"] == Status.DONE
            and r["transfer_status"] == Status.DONE
            and r["fluent_status"] == Status.COMPLETED
        )
        computing = sum(
            1 for r in rows
            if r["fluent_status"] == Status.COMPUTING
        )
        error = sum(
            1 for r in rows
            if r["sw_status"] == Status.ERROR
            or r["sc_status"] == Status.ERROR
            or r["transfer_status"] == Status.ERROR
            or r["fluent_status"] == Status.ERROR
        )
        in_progress = sum(
            1 for r in rows
            if r["fluent_status"] == Status.COMPUTING
            or r["sw_status"] == Status.IN_PROGRESS
            or r["sc_status"] == Status.IN_PROGRESS
        )
        pending = total - completed - error - in_progress
        return {
            "total": total,
            "completed": completed,
            "computing": computing,
            "in_progress": in_progress,
            "error": error,
            "pending": pending,
        }

    def get_computing_configs(self) -> List[str]:
        """获取所有远程正在计算的构型 ID 列表"""
        rows = self.get_all_states()
        return [r["config_id"] for r in rows if r["fluent_status"] == Status.COMPUTING]

    # -----------------------------------------------------------------------
    # 便捷方法（pipeline_controller.py 直接调用）
    # -----------------------------------------------------------------------
    def initialize_configs(self, configs: List):
        """
        初始化/同步构型列表（兼容 ConfigCombination 与旧 PARAMETER_SETS dict）
        """
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in configs:
                # 兼容 ConfigCombination 数据类
                if hasattr(item, "id"):
                    cid = item.id
                elif isinstance(item, dict):
                    cid = item["config_id"]
                else:
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM task_state WHERE config_id = ?", (cid,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO task_state (config_id, created_at, updated_at) VALUES (?, ?, ?)",
                        (cid, now, now),
                    )
            conn.commit()
        logger.info("已同步 %d 个构型初始状态到数据库", len(configs))

    def get_configs_by_fluent_status(self, status: str) -> List[str]:
        """按 fluent_status 筛选构型 ID 列表"""
        rows = self.get_all_states()
        return [r["config_id"] for r in rows if r["fluent_status"] == status]

    def update_sw_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        """便捷方法：更新 SW 阶段状态"""
        self.update_status(config_id, sw_status=status, error_msg=error_msg)

    def update_sc_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        """便捷方法：更新 SC 阶段状态"""
        self.update_status(config_id, sc_status=status, error_msg=error_msg)

    def update_transfer_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        """便捷方法：更新传输阶段状态"""
        self.update_status(config_id, transfer_status=status, error_msg=error_msg)

    def update_fluent_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        """便捷方法：更新 Fluent 求解阶段状态"""
        self.update_status(config_id, fluent_status=status, error_msg=error_msg)

    def increment_retry(self, config_id: str):
        """便捷方法：将指定构型的重试计数 +1"""
        self.update_status(config_id, increment_retry=True)

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None
