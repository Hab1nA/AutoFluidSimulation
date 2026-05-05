# =============================================================================
# modules/state_manager.py — SQLite 状态管理与断点续传逻辑（阶段1）
#
# 表结构变更说明 (v2.0)：
#   原 fluent_status 字段已拆分为 meshing_status 和 solving_status 两个独立字段，
#   以支持网格划分与求解仿真的精细化管理。
#
#   状态枚举：
#     Pending    — 未开始
#     InProgress — 执行中
#     Done       — 成功完成
#     Computing  — 已提交到远程，等待完成（仅 meshing/solving 使用）
#     Error      — 出错
#     Completed  — 最终完成（仅 solving 使用，等同 Done 但代表全流程结束）
# =============================================================================
import sqlite3
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import LOCAL_CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------
class Status:
    PENDING = "Pending"
    IN_PROGRESS = "InProgress"
    DONE = "Done"
    COMPUTING = "Computing"
    COMPLETED = "Completed"
    TRANSFERRED = "Transferred"
    ERROR = "Error"


# SQLite 建表语句 (v2.0)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_state (
    config_id        TEXT PRIMARY KEY,
    sw_status        TEXT NOT NULL DEFAULT 'Pending',
    sc_status        TEXT NOT NULL DEFAULT 'Pending',
    transfer_status  TEXT NOT NULL DEFAULT 'Pending',
    meshing_status   TEXT NOT NULL DEFAULT 'Pending',
    solving_status   TEXT NOT NULL DEFAULT 'Pending',
    retry_count      INTEGER NOT NULL DEFAULT 0,
    error_msg        TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)
"""

class StateManager:
    """
    SQLite 状态管理器，提供线程安全的断点续传能力。

    用法:
        sm = StateManager()
        sm.initialize_configs(configs)
        state = sm.get_state("R2.5_L30_A15")
        sm.update_meshing_status("R2.5_L30_A15", Status.DONE)
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or LOCAL_CONFIG.get("db_path", "pipeline_state.db")
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_table()

    # -----------------------------------------------------------------------
    # 数据库连接管理
    # -----------------------------------------------------------------------
    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接（惰性创建，同一线程复用）"""
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _ensure_table(self):
        """确保表存在，自动执行 v1→v2 迁移"""
        conn = self._get_connection()
        with self._lock:
            # 检查是否有旧表
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_state'"
            )
            old_table_exists = cursor.fetchone() is not None

            if old_table_exists:
                # 检查旧表结构是否有 fluent_status 列
                cols = conn.execute("PRAGMA table_info(task_state)").fetchall()
                col_names = [c["name"] for c in cols]
                if "fluent_status" in col_names and "meshing_status" not in col_names:
                    logger.info("检测到旧版数据库 schema，正在迁移...")
                    self._migrate_v1_to_v2(conn)
                elif "meshing_status" not in col_names:
                    # 表存在但没有新字段，直接重建
                    logger.info("重建数据库表结构...")
                    conn.execute("DROP TABLE IF EXISTS task_state")
                    conn.execute(CREATE_TABLE_SQL)
                    conn.commit()
            else:
                conn.execute(CREATE_TABLE_SQL)
                conn.commit()

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection):
        """从 v1 schema (fluent_status) 迁移到 v2 (meshing_status + solving_status)"""
        # 创建新表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_state_new (
                config_id        TEXT PRIMARY KEY,
                sw_status        TEXT NOT NULL DEFAULT 'Pending',
                sc_status        TEXT NOT NULL DEFAULT 'Pending',
                transfer_status  TEXT NOT NULL DEFAULT 'Pending',
                meshing_status   TEXT NOT NULL DEFAULT 'Pending',
                solving_status   TEXT NOT NULL DEFAULT 'Pending',
                retry_count      INTEGER NOT NULL DEFAULT 0,
                error_msg        TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

        # 迁移数据
        conn.execute("""
            INSERT OR IGNORE INTO task_state_new 
                (config_id, sw_status, sc_status, transfer_status,
                 meshing_status, solving_status, retry_count, error_msg,
                 created_at, updated_at)
            SELECT 
                config_id, sw_status, sc_status, transfer_status,
                CASE 
                    WHEN fluent_status = 'Computing' THEN 'Computing'
                    WHEN fluent_status = 'Completed' OR fluent_status = 'Done' THEN 'Done'
                    WHEN fluent_status IN ('InProgress', 'Transferred') THEN 'Computing'
                    WHEN fluent_status = 'Error' THEN 'Error'
                    ELSE 'Pending'
                END AS meshing_status,
                CASE 
                    WHEN fluent_status = 'Completed' THEN 'Completed'
                    WHEN fluent_status = 'Done' THEN 'Done'
                    WHEN fluent_status = 'Computing' THEN 'Pending'
                    WHEN fluent_status = 'Error' THEN 'Error'
                    ELSE 'Pending'
                END AS solving_status,
                retry_count, error_msg, created_at, updated_at
            FROM task_state
        """)

        # 原子替换
        conn.execute("DROP TABLE task_state")
        conn.execute("ALTER TABLE task_state_new RENAME TO task_state")
        conn.commit()
        logger.info("数据库迁移完成：fluent_status → meshing_status + solving_status")

    # -----------------------------------------------------------------------
    # 基础 CRUD
    # -----------------------------------------------------------------------
    def get_state(self, config_id: str) -> Optional[Dict[str, Any]]:
        """获取单个构型的完整状态（线程安全）"""
        conn = self._get_connection()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM task_state WHERE config_id = ?", (config_id,)
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_all_states(self) -> List[Dict]:
        """获取所有构型的状态列表（线程安全）"""
        conn = self._get_connection()
        with self._lock:
            rows = conn.execute("SELECT * FROM task_state ORDER BY config_id").fetchall()
        return [dict(r) for r in rows]

    def update_status(
        self,
        config_id: str,
        sw_status: Optional[str] = None,
        sc_status: Optional[str] = None,
        transfer_status: Optional[str] = None,
        meshing_status: Optional[str] = None,
        solving_status: Optional[str] = None,
        error_msg: Optional[str] = None,
        increment_retry: bool = False,
    ):
        """
        更新指定构型的状态字段。只更新传入的非 None 字段。

        支持新字段：meshing_status, solving_status
        保留旧字段兼容：fluent_status 将被映射到 meshing_status
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
            if meshing_status is not None:
                fields.append("meshing_status = ?")
                values.append(meshing_status)
            if solving_status is not None:
                fields.append("solving_status = ?")
                values.append(solving_status)
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
        """将指定构型的所有状态重置为 Pending，重试计数清零"""
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE task_state 
                   SET sw_status='Pending', sc_status='Pending', 
                       transfer_status='Pending', meshing_status='Pending',
                       solving_status='Pending', retry_count=0, 
                       error_msg=NULL, updated_at=?
                   WHERE config_id = ?""",
                (now, config_id),
            )
            conn.commit()

    def is_config_complete(self, config_id: str) -> bool:
        """判断一个构型是否已全部完成（所有阶段均为 Done/Completed）"""
        state = self.get_state(config_id)
        if state is None:
            return False
        return (
            state["sw_status"] == Status.DONE
            and state["sc_status"] == Status.DONE
            and state["transfer_status"] == Status.DONE
            and state["meshing_status"] == Status.DONE
            and state["solving_status"] in (Status.DONE, Status.COMPLETED)
        )

    def get_next_pending_stage(self, config_id: str) -> Optional[str]:
        """
        返回指定构型下一个待执行的阶段名称。
        完全按照流水线顺序判断：
            sw → sc → transfer → meshing → solving

        Returns:
            阶段名称 或 None（全部完成）
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
        if state["meshing_status"] not in (Status.DONE, Status.COMPUTING):
            return "meshing"
        if state["solving_status"] not in (Status.DONE, Status.COMPLETED, Status.COMPUTING):
            return "solving"
        return None

    def get_stats(self) -> Dict:
        """
        获取全局统计信息，用于 TUI 显示。

        Returns:
            {
                'total': 总数,
                'completed': 全部完成数,
                'computing': 远程执行中数,
                'in_progress': 本地执行中数,
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
            and r["meshing_status"] == Status.DONE
            and r["solving_status"] in (Status.DONE, Status.COMPLETED)
        )
        computing = sum(
            1 for r in rows
            if r["meshing_status"] == Status.COMPUTING
            or r["solving_status"] == Status.COMPUTING
        )
        error = sum(
            1 for r in rows
            if r["sw_status"] == Status.ERROR
            or r["sc_status"] == Status.ERROR
            or r["transfer_status"] == Status.ERROR
            or r["meshing_status"] == Status.ERROR
            or r["solving_status"] == Status.ERROR
        )
        in_progress = sum(
            1 for r in rows
            if r["meshing_status"] == Status.COMPUTING
            or r["solving_status"] == Status.COMPUTING
            or r["sw_status"] == Status.IN_PROGRESS
            or r["sc_status"] == Status.IN_PROGRESS
        )
        pending = total - completed - error - in_progress
        # 拆分 computing：网格划分与求解分开统计
        computing_meshing = sum(
            1 for r in rows
            if r["meshing_status"] == Status.COMPUTING
        )
        computing_solving = sum(
            1 for r in rows
            if r["solving_status"] == Status.COMPUTING
        )
        return {
            "total": total,
            "completed": completed,
            "computing": computing,
            "computing_meshing": computing_meshing,
            "computing_solving": computing_solving,
            "in_progress": in_progress,
            "error": error,
            "pending": pending,
        }

    def get_computing_configs(self) -> List[str]:
        """获取所有远程正在执行（meshing 或 solving Computing）的构型 ID 列表"""
        rows = self.get_all_states()
        return [
            r["config_id"] for r in rows
            if r["meshing_status"] == Status.COMPUTING
            or r["solving_status"] == Status.COMPUTING
        ]

    # -----------------------------------------------------------------------
    # 便捷方法（pipeline_controller.py 直接调用）
    # -----------------------------------------------------------------------
    def initialize_configs(self, configs: List):
        """初始化/同步构型列表到数据库"""
        conn = self._get_connection()
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in configs:
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
                        """INSERT INTO task_state 
                           (config_id, created_at, updated_at) 
                           VALUES (?, ?, ?)""",
                        (cid, now, now),
                    )
            conn.commit()
        logger.info("已同步 %d 个构型初始状态到数据库", len(configs))

    # -----------------------------------------------------------------------
    # 按阶段筛选的便捷方法
    # -----------------------------------------------------------------------
    def get_configs_by_meshing_status(self, status: str) -> List[str]:
        """按 meshing_status 筛选构型 ID 列表"""
        rows = self.get_all_states()
        return [r["config_id"] for r in rows if r["meshing_status"] == status]

    def get_configs_by_solving_status(self, status: str) -> List[str]:
        """按 solving_status 筛选构型 ID 列表"""
        rows = self.get_all_states()
        return [r["config_id"] for r in rows if r["solving_status"] == status]

    # -----------------------------------------------------------------------
    # 便捷方法
    # -----------------------------------------------------------------------
    def update_sw_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        self.update_status(config_id, sw_status=status, error_msg=error_msg)

    def update_sc_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        self.update_status(config_id, sc_status=status, error_msg=error_msg)

    def update_transfer_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        self.update_status(config_id, transfer_status=status, error_msg=error_msg)

    def update_meshing_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        self.update_status(config_id, meshing_status=status, error_msg=error_msg)

    def update_solving_status(self, config_id: str, status: str, error_msg: Optional[str] = None):
        self.update_status(config_id, solving_status=status, error_msg=error_msg)

    def increment_retry(self, config_id: str):
        self.update_status(config_id, increment_retry=True)

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    # -----------------------------------------------------------------------
    # pipeline_controller.py / InteractiveShell 补充方法 (v2.0)
    # -----------------------------------------------------------------------
    def _update_field(self, config_id: str, field: str, value: str, error_msg: Optional[str] = None):
        """通用字段更新（供 Shell reset 命令使用）。
        
        Raises:
            ValueError: 传入未知字段名时抛出，拒绝静默失败。
        """
        _ALLOWED_FIELDS = {
            "sw_status", "sc_status", "transfer_status",
            "meshing_status", "solving_status",
        }
        if field not in _ALLOWED_FIELDS:
            raise ValueError(
                f"未知状态字段 '{field}'，允许的字段: {sorted(_ALLOWED_FIELDS)}"
            )
        if field == "sw_status":
            self.update_sw_status(config_id, value, error_msg)
        elif field == "sc_status":
            self.update_sc_status(config_id, value, error_msg)
        elif field == "transfer_status":
            self.update_transfer_status(config_id, value, error_msg)
        elif field == "meshing_status":
            self.update_meshing_status(config_id, value, error_msg)
        elif field == "solving_status":
            self.update_solving_status(config_id, value, error_msg)

    def reset_retry(self, config_id: str):
        """重置重试计数为 0（不改变其他状态）"""
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                "UPDATE task_state SET retry_count=0 WHERE config_id=?",
                (config_id,)
            )
            conn.commit()

    def full_reset(self, config_id: str):
        """完全重置指定构型的所有状态（与 reset_config 等价）"""
        self.reset_config(config_id)

    def get_error_configs(self) -> List[Dict]:
        """获取所有 Error 状态的构型信息"""
        rows = self.get_all_states()
        return [
            dict(r) for r in rows
            if r["sw_status"] == Status.ERROR
            or r["sc_status"] == Status.ERROR
            or r["transfer_status"] == Status.ERROR
            or r["meshing_status"] == Status.ERROR
            or r["solving_status"] == Status.ERROR
        ]

    def get_configs_by_status(self, field: str, status: str) -> List[str]:
        """
        按任意状态字段筛选构型 ID 列表。

        Args:
            field: 字段名 (e.g. 'meshing_status', 'solving_status', 'sw_status')
            status: 状态值 (e.g. Status.COMPUTING)

        Returns:
            匹配的 config_id 列表
        """
        rows = self.get_all_states()
        return [r["config_id"] for r in rows if r.get(field) == status]
