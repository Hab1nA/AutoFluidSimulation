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


def test_setup_logger_uses_toml_rotation_settings(tmp_path, monkeypatch):
    import engine.config as cfg
    from utils.logger import setup_logger

    logger_name = "test.rotation.toml.settings"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    original_engine = dict(cfg.ENGINE_CONFIG)

    def _mock_load(*args, **kwargs):
        return {
            "global_settings": {
                "log_max_bytes": 4096,
                "log_backup_count": 4,
            }
        }

    monkeypatch.delenv("AUTOFLUID_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("AUTOFLUID_LOG_BACKUP_COUNT", raising=False)
    monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
    try:
        assert cfg.reload_config_from_toml() is True
        configured = setup_logger(logger_name, log_file=str(tmp_path / "service.log"))
    finally:
        cfg.ENGINE_CONFIG.clear()
        cfg.ENGINE_CONFIG.update(original_engine)

    file_handlers = [
        handler for handler in configured.handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers
    assert file_handlers[0].maxBytes == 4096
    assert file_handlers[0].backupCount == 4
