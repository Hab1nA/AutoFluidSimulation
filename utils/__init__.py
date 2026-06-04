from utils.logger import get_session_log_dir, init_session, setup_logger

__all__ = [
    "setup_logger", "init_session", "get_session_log_dir",
    "read_model_configs",
]


def __getattr__(name: str) -> object:
    if name == "read_model_configs":
        from utils.excel_reader import read_model_configs

        return read_model_configs
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
