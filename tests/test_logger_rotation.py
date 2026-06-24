from __future__ import annotations

import logging.handlers


def test_setup_logger_uses_rotating_file_handler(tmp_path):
    from utils.logger import setup_logger

    logger_name = "test.rotation.file.handler"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()

    log_file = tmp_path / "service.log"
    configured = setup_logger(logger_name, log_file=str(log_file))

    file_handlers = [
        handler for handler in configured.handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers
    assert file_handlers[0].maxBytes > 0
    assert file_handlers[0].backupCount >= 1
