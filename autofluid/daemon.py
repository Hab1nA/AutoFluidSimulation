"""后台守护进程入口。"""

from __future__ import annotations

import signal

from .config import CONFIG
from .engine import PipelineEngine
from .ipc import IPCServer
from .logger import setup_logging
from .state_store import StateStore


def run_daemon() -> None:
    """启动后台守护进程。"""

    logger = setup_logging(CONFIG.local.log_dir)
    store = StateStore(CONFIG.db_path)
    engine = PipelineEngine(CONFIG, store, logger)
    server = IPCServer(CONFIG.ipc_host, CONFIG.ipc_port, engine.handle_command)
    engine.attach_ipc_server(server)
    engine.start_background()

    def _signal_handler(*_args) -> None:
        engine.shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("后台守护进程已启动，监听 %s:%s", CONFIG.ipc_host, CONFIG.ipc_port)
    try:
        server.serve_forever()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run_daemon()
