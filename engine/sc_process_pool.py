"""
SpaceClaim 进程池 — 常驻模式实现。

SpaceClaim 进程在处理完一个构型后不退出，等待下一个构型命令。
通过文件协议 IPC 与 Bridge 通信，消除每次 15-30s 的 GUI 启动开销。

架构：
  SCProcessPool → PersistentSlot(最多 MAX_SLOTS 个)
    → Bridge(--persistent) → SpaceClaim.exe(/RunScript)
    → transit.py(_persistent_loop: 轮询命令文件 → 执行转换 → 写入结果文件)

文件协议 IPC：
  命令: {cmd_dir}/sc_cmd_{slot_id}.json              -> {"command":"process","run_id":"...","config":3,...}
  结果: {cmd_dir}/sc_result_{slot_id}_{run_id}.json  -> {"config":"3","run_id":"...","success":true,...}
  就绪: {cmd_dir}/sc_ready_{slot_id}.json            -> {"ready":true,"slot_id":0}
  退出: sc_cmd_{slot_id}.json                        -> {"command":"quit"}
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.scheduler.control import PipelineControl

from engine.config import LOCAL_PATHS, ENGINE_CONFIG, OPERATION_TIMEOUTS, get_step_filename
from utils.log_paths import service_log_dir
from utils.logger import get_session_log_dir, setup_logger
from utils.process_utils import is_process_alive, run_taskkill

logger = setup_logger(__name__)

_BRIDGE_EXIT_CODE_REASONS: dict[int, str] = {
    0: "Success",
    1: "ScriptFailed",
    2: "LaunchFailed",
    3: "OutputValidationFailed",
    4: "InvalidArgs",
    5: "Timeout",
}


def _format_bridge_exit(returncode: int | None) -> str:
    """格式化 Bridge 退出码，保留原始数值并补充 C# 端语义。"""
    if returncode is None:
        return "exit=unknown"
    reason = _BRIDGE_EXIT_CODE_REASONS.get(returncode, "Unknown")
    return f"exit={returncode} ({reason})"


@dataclass
class PersistentSlot:
    """常驻模式的持久进程槽位。"""
    slot_id: int
    process: subprocess.Popen | None = field(default=None, repr=False)
    pid: int | None = None
    spaceclaim_pid: int | None = None
    bridge_log_path: str | None = None
    cmd_dir: str = ""
    status: str = "idle"  # idle | starting | ready | busy
    current_config: int | None = None
    started_at: float | None = None
    configs_processed: int = 0  # 已处理的构型计数（仅用于诊断/监控）


class SCProcessPool:
    """SpaceClaim 进程池，管理最多 MAX_SLOTS 个常驻 SC 实例。

    SpaceClaim 进程在处理完一个构型后不退出，等待下一个构型命令。
    通过文件协议 IPC 与 Bridge 通信，消除每次 15-30s 的 GUI 启动开销。
    """

    MAX_SLOTS = 1

    def __init__(self):
        self._data_dir = LOCAL_PATHS.get("data_dir", "data")
        os.makedirs(self._data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._first_cleanup_done = False
        self._final_cleanup_done = False

        self._bridge_path = LOCAL_PATHS.get("sc_bridge", "")
        self._sc_script = LOCAL_PATHS.get("sc_script", "")
        self._max_slots = max(1, int(ENGINE_CONFIG.get("sc_max_slots", self.MAX_SLOTS)))

        self._persistent_slots: dict[int, PersistentSlot] = {}
        self._persistent_cmd_dir = os.path.join(self._data_dir, "sc_ipc")
        os.makedirs(self._persistent_cmd_dir, exist_ok=True)
        self._next_slot_id = 1  # 自增槽位 ID 计数器
        self.last_error = ""

    @staticmethod
    def _sc_log_extra(config_name: int, slot: PersistentSlot | None = None) -> dict[str, str]:
        """构造 SC 步骤日志的结构化上下文。"""
        extra = {
            "log_category": "step",
            "config_name": str(config_name),
            "step_name": "sc",
        }
        if slot is not None:
            extra["worker_id"] = f"sc-slot-{slot.slot_id}"
        return extra

    def _fail(self, reason: str) -> bool:
        """Record the last SC failure reason and return False."""
        self.last_error = reason
        return False

    def _retire_persistent_slot_after_failure(self, slot: PersistentSlot, reason: str) -> None:
        """Discard a persistent slot that can no longer be trusted."""
        config_name = slot.current_config if slot.current_config is not None else -1
        logger.warning(
            "[SC-Pool] 常驻槽位%s 因 %s 被废弃并重启",
            slot.slot_id,
            reason,
            extra=self._sc_log_extra(config_name, slot),
        )
        with self._lock:
            if self._persistent_slots.get(slot.slot_id) is slot:
                self._cleanup_persistent_slot(slot)
                self._persistent_slots.pop(slot.slot_id, None)
                logger.warning(
                    "[SC-Pool] 常驻槽位%s 已废弃并清理，后续请求将重建槽位",
                    slot.slot_id,
                    extra=self._sc_log_extra(config_name, slot),
                )

    # ==================================================================
    # 执行入口
    # ==================================================================

    def run_config(self, config_name: int,
                   paused_event: threading.Event | None = None,
                   stopped_event: threading.Event | None = None,
                   pipeline_control: PipelineControl | None = None) -> bool:
        """执行 SC 转换：获取/创建常驻槽位 -> 等待就绪 -> 发送命令 -> 等待结果。"""
        self.last_error = ""
        if not self._persistent_enabled():
            logger.info(
                "[SC-Pool] 常驻模式已关闭，使用一次性 Bridge 执行构型%s",
                config_name,
                extra=self._sc_log_extra(config_name),
            )
            return self._run_oneshot_bridge(config_name)

        with self._external_start(pipeline_control, paused_event, stopped_event) as allowed:
            if not allowed:
                reason = "因暂停或停止暂缓（未启动槽位）"
                logger.info(
                    f"[SC-Pool] 构型{config_name} {reason}",
                    extra=self._sc_log_extra(config_name),
                )
                return self._fail(reason)
            with self._lock:
                # ★ 首次 SC 全体清理：延迟到第一个构型实际进入 SC 步骤时才触发，
                #   而非在 SW 阶段或 start_pipeline 时过早执行。
                #   注意：调用 _shutdown_all_internal() 而非 shutdown_all()，
                #   因为当前已持有 _lock，shutdown_all() 会再次获取锁导致死锁。
                if not self._first_cleanup_done:
                    logger.info("[SC-Pool] === 首次全体 SC 进程清理（首个构型进入 SC 步骤时）===")
                    self._shutdown_all_internal()
                    self._first_cleanup_done = True

                slot = self._get_or_create_persistent_slot()
                if slot is None:
                    reason = self.last_error or "无法获取或创建 SpaceClaim 常驻槽位"
                    if self._oneshot_fallback_enabled():
                        logger.warning(
                            "[SC-Pool] %s，退回一次性 Bridge 执行构型%s",
                            reason, config_name,
                            extra=self._sc_log_extra(config_name),
                        )
                        self._oneshot_fallback_reason = reason
                        return self._run_oneshot_bridge(config_name)
                    return self._fail(reason)

        # ★ 槽位就绪等待：在锁外执行，避免长时间阻塞其他线程。
        #   新创建的槽位处于 "starting" 状态，需等待 Bridge 写入就绪文件；
        #   已就绪的槽位直接跳过。
        if slot.status == "starting":
            if not self._wait_for_slot_ready(slot):
                reason = self.last_error or f"常驻槽位{slot.slot_id} 就绪失败"
                if self._oneshot_fallback_enabled():
                    logger.warning(
                        "[SC-Pool] %s，退回一次性 Bridge 执行构型%s",
                        reason, config_name,
                        extra=self._sc_log_extra(config_name, slot),
                    )
                    self._oneshot_fallback_reason = reason
                    return self._run_oneshot_bridge(config_name)
                return self._fail(reason)

        with self._lock:
            # double-check：等待期间槽位可能已被其他操作清理
            if slot.slot_id not in self._persistent_slots:
                reason = f"槽位{slot.slot_id} 在等待期间被清理"
                logger.error(f"[SC-Pool] {reason}")
                return self._fail(reason)
            if stopped_event is not None and stopped_event.is_set():
                reason = "因停止取消（未发送 SC 命令）"
                logger.info(
                    f"[SC-Pool] 构型{config_name} {reason}",
                    extra=self._sc_log_extra(config_name, slot),
                )
                return self._fail(reason)
            if paused_event is not None and paused_event.is_set():
                reason = "因暂停暂缓（未发送 SC 命令）"
                logger.info(
                    f"[SC-Pool] 构型{config_name} {reason}",
                    extra=self._sc_log_extra(config_name, slot),
                )
                return self._fail(reason)
            slot.status = "busy"
            slot.current_config = config_name

        try:
            result = self._send_persistent_command(
                slot, config_name, paused_event, stopped_event, pipeline_control,
            )
            if result:
                slot.configs_processed += 1
            return result
        finally:
            with self._lock:
                if self._persistent_slots.get(slot.slot_id) is slot:
                    slot.status = "ready"
                    slot.current_config = None

    # ==================================================================
    # 全量清理（首次/末次）
    # ==================================================================

    def _oneshot_fallback_enabled(self) -> bool:
        return bool(ENGINE_CONFIG.get("sc_oneshot_fallback_enabled", True))

    def _persistent_enabled(self) -> bool:
        return bool(ENGINE_CONFIG.get("sc_persistent_enabled", True))

    def _run_oneshot_bridge(self, config_name: int) -> bool:
        """使用一次性 Bridge 执行当前构型，作为常驻启动失败的兜底。"""
        fallback_reason = str(getattr(self, "_oneshot_fallback_reason", "") or "")
        self._oneshot_fallback_reason = ""
        if not self._bridge_path or not os.path.exists(self._bridge_path):
            return self._fail("一次性 Bridge 需要 SpaceClaimBridge.exe")

        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]
        scdoc_name = get_step_filename("sc", config_name)
        if not scdoc_name:
            return self._fail(f"无法生成构型{config_name} SCDOC 文件名")

        cmd = [
            self._bridge_path,
            "--script", self._sc_script,
            "--config", str(config_name),
            "--stepdir", step_dir,
            "--scdocdir", scdoc_dir,
            "--scdocname", scdoc_name,
            "--timeout", str(ENGINE_CONFIG["sc_timeout"]),
            "--sc-exe", LOCAL_PATHS["sc_exe"],
        ]
        os.makedirs(scdoc_dir, exist_ok=True)
        bridge_log_dir = self._build_bridge_log_dir()
        log_path = self._build_bridge_log_path(0, bridge_log_dir)
        env = os.environ.copy()
        env["AUTOFLUID_SC_LOG_DIR"] = bridge_log_dir
        env["AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT"] = str(
            OPERATION_TIMEOUTS.get("sc_process_appear_timeout", 120)
        )
        env["AUTOFLUID_SC_GUI_READY_TIMEOUT"] = str(
            OPERATION_TIMEOUTS.get("sc_gui_ready_timeout", 30)
        )
        env["AUTOFLUID_SC_GUI_STABLE_DELAY"] = str(
            OPERATION_TIMEOUTS.get("sc_gui_stable_delay", 15)
        )

        logger.info(
            "[SC-Pool] 一次性 Bridge stdout/stderr 日志: %s",
            log_path,
            extra=self._sc_log_extra(config_name),
        )
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as log_file:
                completed = subprocess.run(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=ENGINE_CONFIG["sc_timeout"] + 90,
                    env=env,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._fail(f"一次性 Bridge 执行失败: {exc}")

        if completed.returncode == 0:
            if fallback_reason:
                logger.warning(
                    "[SC-Pool] 一次性 Bridge 兜底完成构型%s（常驻槽位恢复原因: %s）",
                    config_name,
                    fallback_reason,
                    extra=self._sc_log_extra(config_name),
                )
            else:
                logger.info(
                    "[SC-Pool] 一次性 Bridge 完成构型%s",
                    config_name,
                    extra=self._sc_log_extra(config_name),
                )
            return True

        return self._fail(
            f"一次性 Bridge 构型{config_name} 失败 "
            f"({_format_bridge_exit(completed.returncode)})"
        )

    def shutdown_all(self):
        with self._lock:
            self._shutdown_all_internal()
            # 重置首次清理标志：下次进入 SC 阶段时需重新清理残留进程
            # （修复：stop() 后同一 Daemon 再次 start 时跳过清理的问题）
            self._first_cleanup_done = False
            # ★ 同步重置末次清理标志：stop() 后的下次运行需重新执行 final cleanup
            self._final_cleanup_done = False

    def _shutdown_all_internal(self):
        """全量清理（内部版本，调用方须已持有 _lock）。"""
        logger.info("[SC-Pool] 执行全量 SpaceClaim 进程清理...")
        for slot in self._persistent_slots.values():
            self._shutdown_persistent_slot(slot)
        self._kill_all_sc_processes()
        logger.info("[SC-Pool] 全量清理完成")

    def _kill_all_sc_processes(self):
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "SpaceClaim.exe"],
                capture_output=True, timeout=10,
            )
            time.sleep(3)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"[SC-Pool] taskkill 异常: {e}")
        self._cleanup_stale_runningcad_markers()

    def _cleanup_stale_runningcad_markers(self) -> None:
        """删除 ANSYS RunningCADs 中指向已退出 SpaceClaim PID 的运行态标记。"""
        running_cads_dir = os.path.join(
            os.environ.get("TEMP", ""),
            "Ansys",
            "RunningCADs",
        )
        if not running_cads_dir or not os.path.isdir(running_cads_dir):
            return

        for root, _dirs, files in os.walk(running_cads_dir):
            for filename in files:
                if not filename.endswith(".direct"):
                    continue
                marker_path = os.path.join(root, filename)
                try:
                    pid = int(os.path.splitext(filename)[0])
                except ValueError:
                    continue
                if is_process_alive(pid):
                    continue
                try:
                    os.remove(marker_path)
                    logger.debug("[SC-Pool] 已清理 stale RunningCADs 标记: %s", marker_path)
                except OSError as exc:
                    logger.debug(
                        "[SC-Pool] 清理 stale RunningCADs 标记失败: %s (%s)",
                        marker_path,
                        exc,
                    )

    def do_first_cleanup(self):
        with self._lock:
            if not self._first_cleanup_done:
                logger.info("[SC-Pool] === 首次全体 SC 进程清理（进入 SC 阶段前）===")
                self._shutdown_all_internal()
                self._first_cleanup_done = True

    def do_final_cleanup(self):
        with self._lock:
            if not self._final_cleanup_done:
                logger.info("[SC-Pool] === 末次全体 SC 进程清理（SC 阶段全部完成后）===")
                self._shutdown_all_internal()
                self._final_cleanup_done = True

    def reset(self):
        with self._lock:
            self._first_cleanup_done = False
            self._final_cleanup_done = False
            for slot in self._persistent_slots.values():
                self._cleanup_persistent_slot(slot)
            self._persistent_slots.clear()
            self._next_slot_id = 1
            logger.info("[SC-Pool] 已重置：常驻进程全部归零")

    # ==================================================================
    # 常驻槽位管理
    # ==================================================================

    def _get_or_create_persistent_slot(self) -> PersistentSlot | None:
        """获取空闲的常驻槽位，若无则创建新槽位。调用方须持有 _lock。

        常驻进程会一直运行直到被 quit 命令关闭或进程异常退出。
        不会基于已处理构型数主动回收——只要文档关闭逻辑正常工作，
        SpaceClaim 应能无限期稳定运行。
        """
        ready_slots: list[PersistentSlot] = []

        for slot in list(self._persistent_slots.values()):
            if slot.status == "ready":
                if slot.process is not None and slot.process.poll() is None:
                    ready_slots.append(slot)
                else:
                    logger.warning(
                        f"[SC-Pool] 常驻槽位{slot.slot_id} 进程已死亡，清理并移除 "
                        f"(Bridge PID={slot.pid}, SpaceClaim PID={slot.spaceclaim_pid})"
                    )
                    self._cleanup_persistent_slot(slot)
                    # 从字典中移除孤立槽位，防止长期累积
                    self._persistent_slots.pop(slot.slot_id, None)
                    logger.warning(
                        "[SC-Pool] 死亡常驻槽位%s 已清理，后续请求将创建新槽位",
                        slot.slot_id,
                    )
            elif slot.status == "busy":
                if slot.process is not None and slot.process.poll() is not None:
                    logger.warning(
                        f"[SC-Pool] 常驻槽位{slot.slot_id} busy 但进程已死亡，清理并重新启动 "
                        f"(Bridge PID={slot.pid}, SpaceClaim PID={slot.spaceclaim_pid})"
                    )
                    self._cleanup_persistent_slot(slot)
                    # ★ 重新启动进程：不能直接返回已清理的空槽位，
                    #    否则后续 _send_persistent_command 会在无进程的情况下
                    #    空等 300s 超时。重新启动后 slot 进入 "starting" 状态，
                    #    run_config() 会通过 _wait_for_slot_ready 等待就绪。
                    if self._launch_persistent_process(slot):
                        logger.warning(
                            "[SC-Pool] 常驻槽位%s 重启成功: Bridge PID=%s",
                            slot.slot_id,
                            slot.pid,
                        )
                        return slot
                    else:
                        # 启动失败，移除槽位，让后续逻辑创建新槽位
                        self._persistent_slots.pop(slot.slot_id, None)

        if ready_slots:
            return min(
                ready_slots,
                key=lambda s: (
                    s.configs_processed,
                    s.started_at if s.started_at is not None else 0.0,
                    s.slot_id,
                ),
            )

        active_slots = sum(
            1 for s in self._persistent_slots.values()
            if s.status in ("starting", "ready", "busy")
            and (s.process is None or s.process.poll() is None)
        )
        if active_slots >= self._max_slots:
            logger.warning(f"[SC-Pool] 常驻槽位已满 ({active_slots}/{self._max_slots})")
            return None

        slot_id = self._next_slot_id
        self._next_slot_id += 1
        slot = PersistentSlot(slot_id=slot_id, cmd_dir=self._persistent_cmd_dir)
        self._persistent_slots[slot_id] = slot

        if not self._launch_persistent_process(slot):
            return None

        return slot

    def _launch_persistent_process(self, slot: PersistentSlot) -> bool:
        """启动常驻 Bridge 进程。调用方须持有 _lock。"""
        if not self._bridge_path or not os.path.exists(self._bridge_path):
            reason = "常驻模式需要 Bridge (SpaceClaimBridge.exe)"
            logger.error("[SC-Pool] %s", reason)
            return self._fail(reason)

        cmd = [
            self._bridge_path,
            "--persistent",
            "--script", self._sc_script,
            "--cmddir", self._persistent_cmd_dir,
            "--slotid", str(slot.slot_id),
            "--sc-exe", LOCAL_PATHS["sc_exe"],
        ]

        slot.status = "starting"

        sc_env = os.environ.copy()
        sc_env["AUTOFLUID_SC_NOEXIT"] = "1"
        sc_env["AUTOFLUID_SC_PERSISTENT"] = "1"
        sc_env["AUTOFLUID_SC_CMD_DIR"] = self._persistent_cmd_dir
        sc_env["AUTOFLUID_SC_SLOT_ID"] = str(slot.slot_id)
        sc_env["AUTOFLUID_SC_EXE"] = str(LOCAL_PATHS["sc_exe"])
        bridge_log_dir = self._build_bridge_log_dir()
        sc_env["AUTOFLUID_SC_LOG_DIR"] = bridge_log_dir

        # 传递 SC 启动超时配置给 Bridge
        sc_env["AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT"] = str(
            OPERATION_TIMEOUTS.get("sc_process_appear_timeout", 120)
        )
        sc_env["AUTOFLUID_SC_GUI_READY_TIMEOUT"] = str(
            OPERATION_TIMEOUTS.get("sc_gui_ready_timeout", 30)
        )
        sc_env["AUTOFLUID_SC_GUI_STABLE_DELAY"] = str(
            OPERATION_TIMEOUTS.get("sc_gui_stable_delay", 15)
        )
        sc_env["AUTOFLUID_SC_PERSISTENT_READY_TIMEOUT"] = str(
            ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)
        )

        logger.info(f"[SC-Pool] 启动常驻 Bridge: 槽位{slot.slot_id}")
        logger.debug(f"[SC-Pool] 命令: {' '.join(cmd)}")

        self._cleanup_ipc_files(slot.slot_id)

        # ★ 显式删除旧 ready 文件：防止前次运行的残留文件误导就绪检测
        ready_file = os.path.join(self._persistent_cmd_dir, f"sc_ready_{slot.slot_id}.json")
        try:
            if os.path.exists(ready_file):
                os.remove(ready_file)
                logger.debug(f"[SC-Pool] 已清理旧 ready 文件: {ready_file}")
        except OSError:
            pass

        try:
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            bridge_log_path = self._build_bridge_log_path(slot.slot_id, bridge_log_dir)
            slot.bridge_log_path = bridge_log_path
            logger.info(f"[SC-Pool] Bridge stdout/stderr 日志: {bridge_log_path}")
            with open(bridge_log_path, "a", encoding="utf-8", errors="replace") as log_file:
                process = subprocess.Popen(
                    cmd, stdout=log_file, stderr=subprocess.STDOUT,
                    creationflags=creation_flags, env=sc_env,
                )
            slot.process = process
            slot.pid = process.pid
            slot.started_at = time.time()
            logger.info(f"[SC-Pool] 常驻 Bridge PID={process.pid}")
            return True

        except OSError as e:
            reason = f"常驻 Bridge 启动失败: {e}"
            logger.error(f"[SC-Pool] {reason}")
            self._cleanup_persistent_slot(slot)
            return self._fail(reason)

    def _wait_for_slot_ready(self, slot: PersistentSlot) -> bool:
        """等待槽位就绪（不持有 _lock，由调用方在锁外调用）。

        新创建的槽位处于 "starting" 状态，Bridge 启动 SpaceClaim 后
        会写入 ready 文件。本方法轮询等待 ready 文件出现。

        Returns:
            True 表示槽位已就绪，False 表示超时或进程异常退出。
        """
        ready_file = os.path.join(self._persistent_cmd_dir, f"sc_ready_{slot.slot_id}.json")
        ready_timeout = ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)
        deadline = time.time() + ready_timeout

        logger.info(f"[SC-Pool] 等待槽位{slot.slot_id} 就绪 (超时 {ready_timeout}s)...")

        while time.time() < deadline:
            # 进程存活检查
            if slot.process is not None and slot.process.poll() is not None:
                reason = (
                    f"常驻 Bridge 槽位{slot.slot_id} 启动失败 "
                    f"({_format_bridge_exit(slot.process.returncode)})"
                )
                logger.error(
                    f"[SC-Pool] {reason}"
                )
                with self._lock:
                    self._cleanup_persistent_slot(slot)
                return self._fail(reason)
            # ready 文件检查
            if os.path.exists(ready_file):
                logger.info(f"[SC-Pool] 常驻槽位{slot.slot_id} 就绪")
                with self._lock:
                    self._load_bridge_monitor_info(slot)
                    slot.status = "ready"
                return True
            time.sleep(2)

        reason = f"常驻槽位{slot.slot_id} 就绪超时 ({ready_timeout}s)"
        logger.error(f"[SC-Pool] {reason}")
        with self._lock:
            self._cleanup_persistent_slot(slot)
        return self._fail(reason)

    # ==================================================================
    # 命令发送与结果等待
    # ==================================================================

    @contextmanager
    def _external_start(
        self,
        pipeline_control: PipelineControl | None,
        paused_event: threading.Event | None,
        stopped_event: threading.Event | None,
    ) -> Iterator[bool]:
        """锁定一个 SC 槽位或命令启动窗口。"""
        if pipeline_control is not None:
            with pipeline_control.external_start() as allowed:
                yield allowed
            return
        paused = paused_event is not None and paused_event.is_set()
        stopped = stopped_event is not None and stopped_event.is_set()
        yield not paused and not stopped

    def _send_persistent_command(self, slot: PersistentSlot, config_name: int,
                                  paused_event: threading.Event | None,
                                  stopped_event: threading.Event | None,
                                  pipeline_control: PipelineControl | None = None) -> bool:
        """向常驻进程发送命令，通过 SCDOC 文件检测判定完成。

        ★ 核心设计：与 SW 步骤的 FileStableDetector 统一，直接轮询
        SCDOC 输出文件。SaveAs() 是 SpaceClaim 内核操作，完成后
        SCDOC 文件立即可用——不需要等脚本写结果文件、不需要等
        window.Close()。

        完成条件：SCDOC 文件存在 + mtime >= 命令发送时刻 + 大小稳定 3s。
        失败信号：脚本写入 success=false 的结果文件 → 提前返回。
        """
        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]
        scdoc_name = get_step_filename("sc", config_name)

        if not scdoc_name:
            reason = f"无法生成构型{config_name} SCDOC 文件名"
            logger.error(
                f"[SC-Pool] {reason}",
                extra=self._sc_log_extra(config_name, slot),
            )
            return self._fail(reason)

        scdoc_file = os.path.join(scdoc_dir, scdoc_name)
        os.makedirs(scdoc_dir, exist_ok=True)

        # per-run 结果文件（仅用于接收错误信号）
        run_id = uuid.uuid4().hex[:12]
        cmd_file = os.path.join(self._persistent_cmd_dir, f"sc_cmd_{slot.slot_id}.json")
        result_file = os.path.join(self._persistent_cmd_dir,
                                   f"sc_result_{slot.slot_id}_{run_id}.json")

        cmd_data = {
            "command": "process",
            "run_id": run_id,
            "config": config_name,
            "stepdir": step_dir,
            "scdocdir": scdoc_dir,
            "scdocname": scdoc_name,
        }
        with self._external_start(pipeline_control, paused_event, stopped_event) as allowed:
            if not allowed:
                reason = "因暂停或停止暂缓（未发送 SC 命令）"
                logger.info(
                    f"[SC-Pool] 构型{config_name} {reason}",
                    extra=self._sc_log_extra(config_name, slot),
                )
                return self._fail(reason)
            # 仅接受命令发送后创建或修改的 SCDOC 文件。
            command_sent_at = time.time()
            try:
                with open(cmd_file, "w") as f:
                    json.dump(cmd_data, f)
            except OSError as e:
                reason = f"写入命令文件失败: {e}"
                logger.error(
                    f"[SC-Pool] {reason}",
                    extra=self._sc_log_extra(config_name, slot),
                )
                return self._fail(reason)

        logger.info(
            f"[SC-Pool] 构型{config_name} 命令已发送 (槽位{slot.slot_id}, run={run_id})",
            extra=self._sc_log_extra(config_name, slot),
        )

        timeout = ENGINE_CONFIG["sc_timeout"]
        deadline = time.time() + timeout
        poll_interval = OPERATION_TIMEOUTS["sc_poll_interval"]
        scdoc_stable_seconds = ENGINE_CONFIG.get("sc_scdoc_stable_seconds", 3.0)

        # 用于稳定性检测的上一次采样
        last_size = -1
        last_size_stable_since = 0.0
        pause_logged = False

        while True:
            # ---- 进程存活检查 ----
            if slot.process is not None and slot.process.poll() is not None:
                reason = (
                    f"常驻 Bridge 槽位{slot.slot_id} 意外退出 "
                    f"({_format_bridge_exit(slot.process.returncode)})"
                )
                logger.error(f"[SC-Pool] {reason}")
                self._cleanup_run_files(slot.slot_id, run_id)
                self._retire_persistent_slot_after_failure(slot, reason)
                return self._fail(reason)

            # ---- 停止检查 ----
            # pause 语义：命令一旦发送给 SpaceClaim，当前构型继续跑完；
            # worker pool 会在 SC 完成后停在 Transfer 前，并在暂停期间不再取新任务。
            if paused_event is not None and paused_event.is_set():
                if not pause_logged:
                    logger.info(
                        f"[SC-Pool] 构型{config_name} (run={run_id}) 已收到暂停，"
                        "等待当前 SpaceClaim 命令自然完成（不中断进行中的转换）",
                        extra=self._sc_log_extra(config_name, slot),
                    )
                    pause_logged = True

            if stopped_event is not None and stopped_event.is_set():
                reason = "因停止取消"
                logger.info(
                    f"[SC-Pool] 构型{config_name} (run={run_id}) {reason}",
                    extra=self._sc_log_extra(config_name, slot),
                )
                self._cleanup_run_files(slot.slot_id, run_id)
                return self._fail(reason)

            # ---- 错误提前信号：脚本写入 success=false 的结果文件 ----
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r") as f:
                        result_data = json.load(f)
                    success = result_data.get("success", False)
                    message = result_data.get("message", "")
                    try:
                        os.remove(result_file)
                    except OSError:
                        pass
                    if not success:
                        reason = f"脚本报错: {message}"
                        logger.error(
                            f"[SC-Pool] FAIL 构型{config_name} {reason}",
                            extra=self._sc_log_extra(config_name, slot),
                        )
                        return self._fail(reason)
                except (ValueError, IOError, OSError):
                    try:
                        os.remove(result_file)
                    except OSError:
                        pass

            # ---- 主判定：SCDOC 文件检测（与 SW 步骤统一） ----
            if os.path.exists(scdoc_file):
                try:
                    file_mtime = os.path.getmtime(scdoc_file)
                    file_size = os.path.getsize(scdoc_file)
                except OSError:
                    time.sleep(poll_interval)
                    continue

                # mtime 早于命令发送 → 旧文件残留，跳过
                if file_mtime < command_sent_at - 1.0:
                    time.sleep(poll_interval)
                    continue

                if file_size <= 0:
                    time.sleep(poll_interval)
                    continue

                # 稳定性检测：大小在 scdoc_stable_seconds 内不变
                now = time.time()
                if file_size != last_size:
                    last_size = file_size
                    last_size_stable_since = now
                elif now - last_size_stable_since >= scdoc_stable_seconds:
                    # SCDOC 文件大小已稳定 → SaveAs 完成
                    logger.info(
                        f"[SC-Pool] OK 构型{config_name} SCDOC: "
                        f"{os.path.basename(scdoc_file)} ({file_size} bytes)",
                        extra=self._sc_log_extra(config_name, slot),
                    )
                    self._cleanup_run_files(slot.slot_id, run_id)
                    return True

            # ---- 超时 ----
            if time.time() >= deadline:
                # 最后一次检查：SCDOC 可能在超时瞬间刚好完成
                if os.path.exists(scdoc_file):
                    try:
                        mt = os.path.getmtime(scdoc_file)
                        sz = os.path.getsize(scdoc_file)
                        if mt >= command_sent_at - 1.0 and sz > 0:
                            logger.warning(
                                f"[SC-Pool] 构型{config_name} 超时但 SCDOC 已存在，接受 "
                                f"({sz} bytes)",
                                extra=self._sc_log_extra(config_name, slot),
                            )
                            self._cleanup_run_files(slot.slot_id, run_id)
                            return True
                    except OSError:
                        pass
                reason = f"构型{config_name} 超时 ({timeout}s), run={run_id}"
                logger.error(f"[SC-Pool] {reason}", extra=self._sc_log_extra(config_name, slot))
                self._cleanup_run_files(slot.slot_id, run_id)
                self._retire_persistent_slot_after_failure(slot, reason)
                return self._fail(reason)

            time.sleep(poll_interval)

    # ==================================================================
    # 槽位生命周期
    # ==================================================================

    def _cleanup_run_files(self, slot_id: int, run_id: str) -> None:
        """清理单次运行的结果文件（不清理 ready 文件和槽位级命令文件）。"""
        for suffix in [f"sc_result_{slot_id}_{run_id}.json",
                       f"sc_result_{slot_id}_{run_id}.json.tmp"]:
            path = os.path.join(self._persistent_cmd_dir, suffix)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _build_bridge_log_dir(self) -> str:
        """构建 Bridge 与 transit 共用的会话日志目录。"""
        session_log_dir = get_session_log_dir()
        bridge_log_dir = (
            os.path.join(session_log_dir, "bridge")
            if session_log_dir
            else service_log_dir("spaceclaim")
        )
        os.makedirs(bridge_log_dir, exist_ok=True)
        return bridge_log_dir

    def _build_bridge_log_path(self, slot_id: int, bridge_log_dir: str | None = None) -> str:
        """构建 Bridge stdout/stderr 捕获日志路径。"""
        if bridge_log_dir is None:
            bridge_log_dir = self._build_bridge_log_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(
            bridge_log_dir,
            f"spaceclaim_bridge_slot{slot_id}_{timestamp}.log",
        )

    def _load_bridge_monitor_info(self, slot: PersistentSlot) -> None:
        """读取 Bridge 写出的实际 SpaceClaim 监控 PID。"""
        monitor_file = os.path.join(
            self._persistent_cmd_dir,
            f"sc_bridge_{slot.slot_id}.json",
        )
        if not os.path.exists(monitor_file):
            logger.warning(
                f"[SC-Pool] 常驻槽位{slot.slot_id} 未找到 Bridge 监控信息文件"
            )
            return

        try:
            with open(monitor_file, encoding="utf-8") as f:
                data = json.load(f)
            spaceclaim_pid = data.get("spaceclaim_pid")
            if isinstance(spaceclaim_pid, int):
                slot.spaceclaim_pid = spaceclaim_pid
            bridge_pid = data.get("bridge_pid")
            status = data.get("status", "")
            logger.info(
                f"[SC-Pool] 常驻槽位{slot.slot_id} 监控 SpaceClaim PID="
                f"{slot.spaceclaim_pid} (Bridge PID={bridge_pid}, status={status})"
            )
        except (OSError, ValueError, TypeError) as e:
            logger.warning(
                f"[SC-Pool] 读取槽位{slot.slot_id} Bridge 监控信息失败: {e}"
            )

    def _cleanup_ipc_files(self, slot_id: int) -> None:
        """清理槽位相关的所有 IPC 文件（命令、结果、就绪标志）。

        用于槽位整体关闭/重置场景（shutdown_all / reset），
        不在单次命令超时时调用——避免误删 ready 文件。
        """
        # 兼容清理：旧格式固定结果文件 + 本次改动后不再产生的 per-run 结果
        for suffix in [f"sc_cmd_{slot_id}.json",
                       f"sc_result_{slot_id}.json",
                       f"sc_result_{slot_id}.json.tmp",
                       f"sc_ready_{slot_id}.json",
                       f"sc_bridge_{slot_id}.json"]:
            path = os.path.join(self._persistent_cmd_dir, suffix)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        # 清理所有 per-run 结果文件
        try:
            prefix = f"sc_result_{slot_id}_"
            for name in os.listdir(self._persistent_cmd_dir):
                if name.startswith(prefix) and name.endswith(".json"):
                    try:
                        os.remove(os.path.join(self._persistent_cmd_dir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    def _cleanup_persistent_slot(self, slot: PersistentSlot) -> None:
        """清理常驻槽位（终止进程、清理文件）。调用方须持有 _lock。"""
        if slot.spaceclaim_pid is not None:
            run_taskkill(slot.spaceclaim_pid)
        if slot.process is not None:
            try:
                if slot.pid is not None:
                    run_taskkill(slot.pid)
                else:
                    slot.process.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                slot.process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                pass
        slot.process = None
        slot.pid = None
        slot.spaceclaim_pid = None
        slot.status = "idle"
        slot.current_config = None
        slot.configs_processed = 0  # ★ 重置构型计数
        self._cleanup_ipc_files(slot.slot_id)

    def _shutdown_persistent_slot(self, slot: PersistentSlot) -> None:
        """关闭常驻槽位（发送 quit 命令，等待退出，兜底 taskkill）。"""
        if slot.process is None:
            return
        quit_file = os.path.join(self._persistent_cmd_dir, f"sc_cmd_{slot.slot_id}.json")
        try:
            with open(quit_file, "w") as f:
                json.dump({"command": "quit"}, f)
        except OSError:
            pass
        try:
            slot.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                if slot.spaceclaim_pid is not None:
                    run_taskkill(slot.spaceclaim_pid)
                if slot.pid is not None:
                    run_taskkill(slot.pid)
                else:
                    slot.process.kill()
                slot.process.communicate(timeout=5)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                pass
        slot.process = None
        slot.pid = None
        slot.spaceclaim_pid = None
        slot.status = "idle"
        self._cleanup_ipc_files(slot.slot_id)

    # ==================================================================
    # 状态查询
    # ==================================================================

    def get_pool_status(self) -> dict:
        with self._lock:
            ready_count = sum(1 for s in self._persistent_slots.values() if s.status == "ready")
            busy_count = sum(1 for s in self._persistent_slots.values() if s.status == "busy")
            return {
                "max_slots": self._max_slots,
                "ready": ready_count,
                "busy": busy_count,
                "slots": {
                    sid: {"slot_id": s.slot_id, "status": s.status,
                          "pid": s.pid, "current_config": s.current_config,
                          "spaceclaim_pid": s.spaceclaim_pid,
                          "bridge_log_path": s.bridge_log_path,
                          "configs_processed": s.configs_processed}
                    for sid, s in self._persistent_slots.items()
                },
            }
