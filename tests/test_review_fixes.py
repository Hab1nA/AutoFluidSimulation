import logging
import queue
import threading
from unittest.mock import Mock

import paramiko

from engine.config import STATUS_COMPLETED, STATUS_ERROR, STATUS_WAITING
from engine.scheduler.meshing_monitor import MeshingMonitor
from engine.scheduler.worker_pool import WorkerPoolManager
from utils import logger as logger_mod
from utils.ssh_client import RemoteWorkstation


class _DummyState:
    def __init__(self, statuses):
        self._statuses = dict(statuses)
        self.messages = {}

    def get_step_status(self, config_name, step_name):
        return self._statuses[(config_name, step_name)]

    def set_step_status(self, config_name, step_name, status, message=""):
        self._statuses[(config_name, step_name)] = status
        self.messages[(config_name, step_name)] = message


def _build_worker_pool(state):
    return WorkerPoolManager(
        state_manager=state,
        task_runner=Mock(),
        sc_queue=queue.Queue(),
        paused_event=threading.Event(),
        stopped_event=threading.Event(),
        barrier_passed_event=threading.Event(),
        retry_manager=Mock(),
    )


def test_flush_deferred_loggers_preserves_original_format(tmp_path):
    formatter = logging.Formatter("%(levelname)s:%(message)s")
    logger = logging.Logger("test.review.flush")
    buf_handler = logger_mod._BufferHandler(formatter)
    logger.addHandler(buf_handler)

    record = logging.LogRecord(
        name=logger.name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="buffered line",
        args=(),
        exc_info=None,
    )
    buf_handler.emit(record)

    old_session_log_dir = logger_mod._session_log_dir
    old_deferred = list(logger_mod._deferred_loggers)
    try:
        logger_mod._session_log_dir = str(tmp_path)
        logger_mod._deferred_loggers[:] = [(logger, buf_handler, formatter)]
        logger_mod._flush_deferred_loggers()
    finally:
        logger_mod._session_log_dir = old_session_log_dir
        logger_mod._deferred_loggers[:] = old_deferred

    log_file = tmp_path / f"{logger.name}.log"
    assert log_file.read_text(encoding="utf-8").splitlines() == ["WARNING:buffered line"]


def test_is_connected_returns_false_on_ssh_exception():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    transport = Mock()
    transport.is_active.return_value = True
    transport.send_ignore.side_effect = paramiko.SSHException("broken transport")

    host._ssh = Mock()
    host._ssh.get_transport.return_value = transport

    assert host.is_connected() is False


def test_mark_meshing_error_prefers_sc_failure_reason():
    state = _DummyState({
        (1, "SC"): STATUS_ERROR,
        (1, "Transfer"): STATUS_ERROR,
        (1, "Meshing"): STATUS_WAITING,
    })
    worker_pool = _build_worker_pool(state)

    worker_pool._mark_meshing_error_if_transfer_failed(1, "SC crashed")

    assert state.get_step_status(1, "Meshing") == STATUS_ERROR
    assert state.messages[(1, "Meshing")] == "上游 SC 失败: SC crashed"


def test_mark_meshing_error_skips_when_transfer_completed():
    state = _DummyState({
        (1, "SC"): STATUS_COMPLETED,
        (1, "Transfer"): STATUS_COMPLETED,
        (1, "Meshing"): STATUS_WAITING,
    })
    worker_pool = _build_worker_pool(state)

    worker_pool._mark_meshing_error_if_transfer_failed(1, "late failure")

    assert state.get_step_status(1, "Meshing") == STATUS_WAITING
    assert (1, "Meshing") not in state.messages


class _TrackingLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        return False


class _LockedSSH:
    def __init__(self, lock):
        self._lock = lock

    def is_connected(self):
        assert self._lock.held
        return True

    def check_remote_file(self, path):
        assert self._lock.held
        return path.endswith(".txt")


class _LockedRemoteExecutor:
    def __init__(self):
        self._ssh_lock = _TrackingLock()
        self._ssh = _LockedSSH(self._ssh_lock)

    def _get_ssh(self):
        return self._ssh


def test_meshing_monitor_checks_remote_outputs_under_ssh_lock():
    monitor = MeshingMonitor(
        state_manager=Mock(),
        remote_executor=_LockedRemoteExecutor(),
        paused_event=threading.Event(),
        stopped_event=threading.Event(),
    )

    assert monitor._check_remote_outputs_exist(1) is True
