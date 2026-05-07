"""日志工具模块 (Logger Utility)
提供统一的日志记录功能，同时输出到文件和控制台。
支持会话管理：每次进程启动时通过 init_session() 创建独立的日志存放目录，
实现按进程类型 (daemon/client) 和启动时间层级化组织日志文件。
"""
import logging
import os
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
        "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
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
