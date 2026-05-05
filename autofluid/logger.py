"""日志工具。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str) -> logging.Logger:
    """初始化日志系统。"""

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("autofluid")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    log_path = Path(log_dir) / "pipeline.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
