"""日志工具模块 (Logger Utility)
提供统一的日志记录功能，同时输出到文件和控制台。
支持会话管理：每次进程启动时通过 init_session() 创建独立的日志存放目录，
实现按进程类型 (daemon/client) 和启动时间层级化组织日志文件。

延迟日志文件创建：
  当 init_session() 尚未被调用时，setup_logger() 不创建文件 handler，
  而是附加 _BufferHandler 将日志缓冲到内存。init_session() 被调用后，
  缓冲的日志回写到文件，确保测试脚本等非会话场景不会产生散落的日志文件。

新增功能：
- LogEntry 数据类：结构化日志条目，支持 IPC 传输
- LogBroadcastHandler：将日志记录存入线程安全环形缓冲区，
  供 TUI 客户端通过 IPC 增量拉取，实现详细日志实时展示。
"""
import collections
import itertools
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

_session_type: str | None = None
_session_timestamp: str | None = None
_session_log_dir: str | None = None


# ---------------------------------------------------------------------------
# 延迟日志文件创建支持
# ---------------------------------------------------------------------------


class _BufferHandler(logging.Handler):
    """临时日志处理器，将格式化后的消息缓冲到列表中。

    当 init_session() 尚未被调用时，setup_logger() 使用此 handler
    代替 FileHandler。init_session() 后，缓冲内容回写到文件。
    """

    def __init__(self, formatter: logging.Formatter):
        super().__init__(level=logging.DEBUG)
        self._formatter = formatter
        self.buffer: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self._formatter.format(record))
        except Exception:
            self.handleError(record)


# (logger, buffer_handler, formatter) — 等待 init_session() 后回写
_deferred_loggers: list[
    tuple[logging.Logger, _BufferHandler, logging.Formatter]
] = []
_deferred_lock = threading.Lock()


def init_session(process_type: str, timestamp: str | None = None) -> str:
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
        if _session_log_dir is None:
            raise RuntimeError("日志会话状态异常：_session_log_dir 未初始化")
        return _session_log_dir

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    _session_type = process_type
    _session_timestamp = timestamp

    base_log_dir = _resolve_base_log_dir()
    _session_log_dir = os.path.join(base_log_dir, process_type, timestamp)
    os.makedirs(_session_log_dir, exist_ok=True)

    # 会话目录已就绪，将之前缓冲的日志回写到文件
    _flush_deferred_loggers()

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


def _infer_log_category(name: str) -> str:
    """根据 logger 名称推断日志分类子目录。

    当 init_session() 尚未被调用时，setup_logger() 的回退路径
    需要知道日志应写入哪个分类目录。此函数根据模块名前缀进行推断：

    - ``executor.*`` → ``"executor"``
    - ``engine.*``、``ipc.*``、``PipelineDaemon`` → ``"daemon"``
    - 其他（含 ``utils.*``）→ ``"daemon"``（默认兜底）
    """
    if name.startswith("executor."):
        return "executor"
    if name.startswith(("engine.", "ipc.")) or name == "PipelineDaemon":
        return "daemon"
    # utils.* 及其他模块默认归入 daemon
    return "daemon"


def setup_logger(name: str, log_file: str | None = None) -> logging.Logger:
    """创建并配置一个 logger 实例。

    若已通过 init_session() 初始化会话，日志文件将存放在会话目录下，
    文件名为 {name}.log；若 log_file 参数被显式指定则使用该路径。

    当 init_session() 尚未被调用且未指定 log_file 时，不创建文件 handler，
    而是附加 _BufferHandler 将日志缓冲到内存。待 init_session() 被调用后，
    缓冲内容回写到文件，避免测试脚本等非会话场景在 logs/ 根目录产生散落文件。
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
            # 会话已初始化，直接创建文件 handler
            log_file = os.path.join(_session_log_dir, f"{name}.log")
        else:
            # 会话未初始化：附加临时 buffer handler，延迟写文件
            buf_handler = _BufferHandler(formatter)
            logger.addHandler(buf_handler)
            with _deferred_lock:
                _deferred_loggers.append((logger, buf_handler, formatter))
            return logger
    else:
        log_parent = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_parent, exist_ok=True)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def _flush_deferred_loggers() -> None:
    """将缓冲的日志回写到文件，在 init_session() 内部调用。"""
    with _deferred_lock:
        deferred = list(_deferred_loggers)
        _deferred_loggers.clear()

    for logger, buf_handler, formatter in deferred:
        log_file = os.path.join(_session_log_dir, f"{logger.name}.log")  # type: ignore[arg-type]
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        # 先写入缓冲内容
        for msg in buf_handler.buffer:
            file_handler.emit(
                logging.LogRecord(
                    name=logger.name, level=logging.DEBUG, pathname="", lineno=0,
                    msg=msg, args=(), exc_info=None,
                )
            )

        # 替换 handler：移除 buffer，添加 file handler
        logger.removeHandler(buf_handler)
        logger.addHandler(file_handler)


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

_CONFIG_SCOPED_LOG_PATTERNS = (
    re.compile(r"构型\s*\d+"),
    re.compile(r"\bconfig(?:_name)?\s*[=:]\s*\d+\b", re.IGNORECASE),
)


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
            if getattr(record, "broadcast", True) is False:
                return
            if self._is_polling_log(record):
                return
            if self._is_config_scoped_log(record):
                return
            entry = self._entry_from_record(record)
            with self._lock:
                self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    def _entry_from_record(self, record: logging.LogRecord) -> LogEntry:
        """Build a structured entry from a log record."""
        msg = self._formatter.format(record)
        raw_msg = f"[{record.name}] {record.getMessage()}"
        return LogEntry(
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

    @staticmethod
    def _is_config_scoped_log(record: logging.LogRecord) -> bool:
        """Return True for log messages tied to a single configuration.

        仅过滤 INFO/DEBUG 级别的构型详情日志，WARNING/ERROR/CRITICAL
        始终放行，确保 TUI 详细日志面板不遗漏关键告警。
        """
        if record.levelno >= logging.WARNING:
            return False
        message = record.getMessage()
        return any(pattern.search(message) for pattern in _CONFIG_SCOPED_LOG_PATTERNS)

    @staticmethod
    def _is_polling_log(record: logging.LogRecord) -> bool:
        """判断日志记录是否来自轮询命令，需在 emit 阶段过滤。

        识别规则：
        1. logger 名称包含 "ipc" 且消息中包含轮询命令名称
        2. 或消息中包含轮询命令名称且上下文表明为 IPC 轮询（如"收到命令:"前缀）

        轮询日志会被过滤，避免 TUI 详细日志界面被频繁的状态查询刷屏。
        非 IPC logger（如 PipelineDaemon）中的轮询命令名称不会被误过滤。
        """
        msg = record.getMessage()
        name_lower = record.name.lower()
        is_ipc_logger = "ipc" in name_lower
        if not is_ipc_logger:
            return False
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

        level_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
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


_MAX_BROADCAST_CAPACITY: int = 2000


def install_broadcast_handler(capacity: int = 1000) -> LogBroadcastHandler:
    """创建并安装全局 LogBroadcastHandler 到 root logger。

    仅应被 Daemon 进程调用。TUI 进程无需安装。
    使用线程锁保护，防止多线程并发安装导致重复 handler。

    Args:
        capacity: 环形缓冲区容量（硬性上限为 2000 条）

    Returns:
        LogBroadcastHandler 实例
    """
    global _broadcast_handler

    with _broadcast_handler_lock:
        if _broadcast_handler is not None:
            return _broadcast_handler

        clamped = min(capacity, _MAX_BROADCAST_CAPACITY)
        _broadcast_handler = LogBroadcastHandler(capacity=clamped)

        root_logger = logging.getLogger()
        root_logger.addHandler(_broadcast_handler)

        return _broadcast_handler
