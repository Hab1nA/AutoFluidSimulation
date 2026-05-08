"""日志工具模块 (Logger Utility)
提供统一的日志记录功能，同时输出到文件和控制台。
支持会话管理：每次进程启动时通过 init_session() 创建独立的日志存放目录，
实现按进程类型 (daemon/client) 和启动时间层级化组织日志文件。

新增功能：
- LogEntry 数据类：结构化日志条目，支持 IPC 传输
- LogBroadcastHandler：将日志记录存入线程安全环形缓冲区，
  供 TUI 客户端通过 IPC 增量拉取，实现详细日志实时展示。
"""
import collections
import itertools
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

_session_type: str | None = None
_session_timestamp: str | None = None
_session_log_dir: str | None = None


def init_session(process_type: str, timestamp: str = None) -> str:
    """初始化日志会话，为当前进程创建独立的日志存放目录。

    必须在首次调用 setup_logger() 之前调用此函数，
    否则 setup_logger() 将回退到旧的扁平目录结构。

    Args:
        process_type: 进程类型，如 "daemon" 或 "client"
        timestamp: 会话时间戳，格式 YYYY-MM-DD_HH-MM-SS；若未指定则自动生成

    Returns:
        会话日志目录的绝对路径
    """
    global _session_type, _session_timestamp, _session_log_dir

    if _session_type is not None:
        return _session_log_dir

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    _session_type = process_type
    _session_timestamp = timestamp

    base_log_dir = _resolve_base_log_dir()
    _session_log_dir = os.path.join(base_log_dir, process_type, timestamp)
    os.makedirs(_session_log_dir, exist_ok=True)

    return _session_log_dir


def build_session_log_dir(process_type: str, timestamp: str) -> str:
    """构建会话日志目录路径（不初始化会话）。

    用于主进程在启动子进程前预先构建子进程的日志目录路径，
    以便重定向子进程的 stdout/stderr。

    Args:
        process_type: 进程类型，如 "daemon" 或 "client"
        timestamp: 会话时间戳

    Returns:
        会话日志目录的绝对路径
    """
    base_log_dir = _resolve_base_log_dir()
    return os.path.join(base_log_dir, process_type, timestamp)


def get_session_log_dir() -> str | None:
    """获取当前会话的日志目录路径，若未初始化会话则返回 None。"""
    return _session_log_dir


def get_session_timestamp() -> str | None:
    """获取当前会话的时间戳，若未初始化会话则返回 None。"""
    return _session_timestamp


def get_session_type() -> str | None:
    """获取当前会话的进程类型，若未初始化会话则返回 None。"""
    return _session_type


def _resolve_base_log_dir() -> str:
    """解析日志根目录路径。"""
    try:
        from engine.config import LOCAL_PATHS as _cfg_local_paths
        base = _cfg_local_paths.get("log_dir", "")
    except (ImportError, AttributeError):
        base = ""
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
        )
    return base


def setup_logger(name: str, log_file: str = None) -> logging.Logger:
    """创建并配置一个 logger 实例。

    若已通过 init_session() 初始化会话，日志文件将存放在会话目录下，
    文件名为 {name}.log；否则回退到旧的扁平目录结构，
    文件名为 {name}_{时间戳}_{PID}.log。
    """

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file is None:
        if _session_log_dir is not None:
            log_file = os.path.join(_session_log_dir, f"{name}.log")
        else:
            log_dir = _resolve_base_log_dir()
            os.makedirs(log_dir, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            pid = os.getpid()
            log_file = os.path.join(log_dir, f"{name}_{timestamp}_{pid}.log")
    else:
        log_parent = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_parent, exist_ok=True)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# ============================================================================
# 结构化日志条目 (用于 IPC 传输)
# ============================================================================

_SOURCE_KEYWORDS = {
    "remote_ps": [
        "SSH", "远程", "remote", "PowerShell", "Start-Process",
        "SFTP", "上传", "下载", "标志文件",
    ],
    "local_ps": [
        "subprocess", "SpaceClaim", "tasklist", "taskkill",
        "SC 脚本", "SC 转换", "STEP 导出",
    ],
    "com": [
        "COM", "SolidWorks", "win32com", "GetActiveObject",
        "Dispatch", "OpenDoc6", "SaveAs", "设计表",
        "ForceRebuildAll", "ShowConfiguration2",
    ],
    "scheduler": [
        "调度", "屏障", "barrier", "pipeline", "流水线",
        "构型", "重试",
    ],
    "ipc": [
        "IPC", "客户端", "连接", "socket",
    ],
}

POLLING_COMMANDS = frozenset({
    "get_all_status",
    "get_log_entries",
    "get_engine_status",
})


def _classify_source(logger_name: str, message: str) -> str:
    """根据 logger 名称和日志消息内容分类日志来源。"""
    name_lower = logger_name.lower()
    msg_lower = message.lower()

    if "ssh_client" in name_lower or "remote" in name_lower:
        return "remote_ps"
    if "scheduler" in name_lower:
        return "scheduler"
    if "ipc" in name_lower:
        return "ipc"
    if "daemon" in name_lower:
        return "system"

    for source, keywords in _SOURCE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in msg_lower:
                return source

    return "system"


@dataclass
class LogEntry:
    """结构化日志条目，用于 IPC 传输和 TUI 展示。

    message: 完整格式化消息（含时间戳、级别、logger名），用于文件导出
    raw_message: 原始消息文本（仅 logger名 + 消息内容），用于 TUI 详细日志面板显示
    """

    id: int
    timestamp: str
    level: str
    source: str
    logger_name: str
    message: str
    raw_message: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "logger_name": self.logger_name,
            "message": self.message,
            "raw_message": self.raw_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LogEntry":
        return cls(
            id=data.get("id", 0),
            timestamp=data.get("timestamp", ""),
            level=data.get("level", "INFO"),
            source=data.get("source", "system"),
            logger_name=data.get("logger_name", ""),
            message=data.get("message", ""),
            raw_message=data.get("raw_message", data.get("message", "")),
        )


class LogBroadcastHandler(logging.Handler):
    """自定义日志处理器，将日志记录存入线程安全环形缓冲区。

    供 IPC 查询接口增量拉取，实现 TUI 详细日志实时展示。
    缓冲区满时自动淘汰最旧条目（deque maxlen 机制）。
    """

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.setLevel(logging.DEBUG)
        self._buffer: collections.deque[LogEntry] = collections.deque(maxlen=capacity)
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._is_polling_log(record):
                return
            msg = self._formatter.format(record)
            raw_msg = f"[{record.name}] {record.getMessage()}"
            entry = LogEntry(
                id=next(self._id_counter),
                timestamp=datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S"),
                level=record.levelname,
                source=_classify_source(record.name, record.getMessage()),
                logger_name=record.name,
                message=msg,
                raw_message=raw_msg,
            )
            with self._lock:
                self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    @staticmethod
    def _is_polling_log(record: logging.LogRecord) -> bool:
        """判断日志记录是否来自轮询命令，需在 emit 阶段过滤。

        识别规则：
        1. logger 名称包含 "ipc"（来自 IPC 子系统）
        2. 日志消息中包含轮询命令名称（get_all_status / get_log_entries / get_engine_status）

        同时满足两个条件才判定为轮询日志，避免误过滤其他模块中
        偶然包含这些命令名的日志。
        """
        if "ipc" not in record.name.lower():
            return False
        msg = record.getMessage()
        for cmd in POLLING_COMMANDS:
            if cmd in msg:
                return True
        return False

    def get_entries(
        self,
        since_id: int = 0,
        limit: int = 100,
        level_filter: str | None = None,
        source_filter: str | None = None,
    ) -> dict:
        """增量查询日志条目。

        Args:
            since_id: 返回 ID 大于此值的所有条目（0 = 从头开始）
            limit: 最大返回条数
            level_filter: 按日志级别过滤（如 "ERROR", "WARNING"）
            source_filter: 按来源过滤（如 "remote_ps", "local_ps"）

        Returns:
            {"entries": [LogEntry.to_dict(), ...], "latest_id": int, "total": int}
        """
        with self._lock:
            snapshot = list(self._buffer)

        filtered = []
        for entry in snapshot:
            if entry.id <= since_id:
                continue
            if level_filter and entry.level != level_filter:
                continue
            if source_filter and entry.source != source_filter:
                continue
            filtered.append(entry)

        filtered.sort(key=lambda e: e.id)

        latest_id = snapshot[-1].id if snapshot else 0
        total = len(snapshot)

        return {
            "entries": [e.to_dict() for e in filtered[:limit]],
            "latest_id": latest_id,
            "total": total,
        }

    def get_stats(self) -> dict:
        """获取日志缓冲区统计信息。"""
        with self._lock:
            snapshot = list(self._buffer)

        level_counts = {}
        source_counts = {}
        for entry in snapshot:
            level_counts[entry.level] = level_counts.get(entry.level, 0) + 1
            source_counts[entry.source] = source_counts.get(entry.source, 0) + 1

        return {
            "total": len(snapshot),
            "latest_id": snapshot[-1].id if snapshot else 0,
            "level_counts": level_counts,
            "source_counts": source_counts,
        }


_broadcast_handler: LogBroadcastHandler | None = None
_broadcast_handler_lock = threading.Lock()


def get_broadcast_handler() -> LogBroadcastHandler | None:
    """获取全局 LogBroadcastHandler 实例（若已创建）。"""
    if _broadcast_handler is not None:
        return _broadcast_handler
    with _broadcast_handler_lock:
        return _broadcast_handler


def install_broadcast_handler(capacity: int = 1000) -> LogBroadcastHandler:
    """创建并安装全局 LogBroadcastHandler 到 root logger。

    仅应被 Daemon 进程调用。TUI 进程无需安装。
    使用线程锁保护，防止多线程并发安装导致重复 handler。

    Args:
        capacity: 环形缓冲区容量

    Returns:
        LogBroadcastHandler 实例
    """
    global _broadcast_handler

    with _broadcast_handler_lock:
        if _broadcast_handler is not None:
            return _broadcast_handler

        _broadcast_handler = LogBroadcastHandler(capacity=capacity)

        root_logger = logging.getLogger()
        root_logger.addHandler(_broadcast_handler)

        return _broadcast_handler
