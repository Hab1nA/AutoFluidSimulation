"""
===============================================================================
日志工具模块 (Logger Utility)
提供统一的日志记录功能，同时输出到文件和控制台。
===============================================================================
"""
import logging
import os
from datetime import datetime


def setup_logger(name: str, log_file: str = None) -> logging.Logger:
    """
    创建并配置一个 logger 实例。

    Args:
        name: logger 名称（通常使用 __name__）
        log_file: 日志文件路径，若为 None 则自动生成到日志目录

    Returns:
        配置好的 logging.Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 日志格式：时间 | 级别 | 模块 | 消息
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出 handler
    if log_file is None:
        # 延迟导入以避免循环依赖：logger 是底层工具，不应在模块顶层依赖 engine.config
        try:
            from engine.config import LOCAL_PATHS as _cfg_local_paths
            log_dir = _cfg_local_paths.get("log_dir", "")
        except (ImportError, AttributeError):
            log_dir = ""
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        else:
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"{name}_{timestamp}.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
