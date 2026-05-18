"""
SpaceClaim 进程池 — 常驻模式实现。

SpaceClaim 进程在处理完一个构型后不退出，等待下一个构型命令。
通过文件协议 IPC 与 Bridge 通信，消除每次 15-30s 的 GUI 启动开销。

架构：
  SCProcessPool → PersistentSlot(最多 MAX_SLOTS 个)
    → Bridge(--persistent) → SpaceClaim.exe(/RunScript)
    → transit.py(_persistent_loop: 轮询命令文件 → 执行转换 → 写入结果文件)

文件协议 IPC：
  命令: {cmd_dir}/sc_cmd_{slot_id}.json    -> {"config":3,"stepdir":"...","scdocdir":"..."}
  结果: {cmd_dir}/sc_result_{slot_id}.json -> {"config":"3","success":true,"message":"..."}
  就绪: {cmd_dir}/sc_ready_{slot_id}.json  -> {"ready":true,"slot_id":0}
  退出: sc_cmd_{slot_id}.json              -> {"command":"quit"}
"""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from engine.config import LOCAL_PATHS, ENGINE_CONFIG, OPERATION_TIMEOUTS, get_step_filename
from utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class PersistentSlot:
    """常驻模式的持久进程槽位。"""
    slot_id: int
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    pid: Optional[int] = None
    cmd_dir: str = ""
    status: str = "idle"  # idle | starting | ready | busy
    current_config: Optional[int] = None
    started_at: Optional[float] = None


class SCProcessPool:
    """SpaceClaim 进程池，管理最多 MAX_SLOTS 个常驻 SC 实例。

    SpaceClaim 进程在处理完一个构型后不退出，等待下一个构型命令。
    通过文件协议 IPC 与 Bridge 通信，消除每次 15-30s 的 GUI 启动开销。
    """

    MAX_SLOTS = 3

    def __init__(self):
        self._data_dir = LOCAL_PATHS.get("data_dir", "data")
        os.makedirs(self._data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._first_cleanup_done = False
        self._final_cleanup_done = False

        self._bridge_path = LOCAL_PATHS.get("sc_bridge", "")
        self._sc_script = LOCAL_PATHS.get("sc_script", "")

        self._persistent_slots: Dict[int, PersistentSlot] = {}
        self._persistent_cmd_dir = os.path.join(self._data_dir, "sc_ipc")
        os.makedirs(self._persistent_cmd_dir, exist_ok=True)

    # ==================================================================
    # 执行入口
    # ==================================================================

    def run_config(self, config_name: int,
                   paused_event: Optional[threading.Event] = None,
                   stopped_event: Optional[threading.Event] = None) -> bool:
        """执行 SC 转换：获取/创建常驻槽位 -> 发送命令 -> 等待结果。"""
        with self._lock:
            slot = self._get_or_create_persistent_slot()
            if slot is None:
                return False
            slot.status = "busy"
            slot.current_config = config_name

        try:
            return self._send_persistent_command(slot, config_name, paused_event, stopped_event)
        finally:
            with self._lock:
                if slot.slot_id in self._persistent_slots:
                    slot.status = "ready"
                    slot.current_config = None

    # ==================================================================
    # 全量清理（首次/末次）
    # ==================================================================

    def shutdown_all(self):
        with self._lock:
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

    def do_first_cleanup(self):
        with self._lock:
            if not self._first_cleanup_done:
                logger.info("[SC-Pool] === 首次全体 SC 进程清理（进入 SC 阶段前）===")
                self.shutdown_all()
                self._first_cleanup_done = True

    def do_final_cleanup(self):
        with self._lock:
            if not self._final_cleanup_done:
                logger.info("[SC-Pool] === 末次全体 SC 进程清理（SC 阶段全部完成后）===")
                self.shutdown_all()
                self._final_cleanup_done = True

    def reset(self):
        with self._lock:
            self._first_cleanup_done = False
            self._final_cleanup_done = False
            for slot in self._persistent_slots.values():
                self._cleanup_persistent_slot(slot)
            self._persistent_slots.clear()
            logger.info("[SC-Pool] 已重置：常驻进程全部归零")

    # ==================================================================
    # 常驻槽位管理
    # ==================================================================

    def _get_or_create_persistent_slot(self) -> Optional[PersistentSlot]:
        """获取空闲的常驻槽位，若无则创建新槽位。调用方须持有 _lock。"""
        for slot in self._persistent_slots.values():
            if slot.status == "ready":
                if slot.process is not None and slot.process.poll() is None:
                    return slot
                else:
                    logger.warning(f"[SC-Pool] 常驻槽位{slot.slot_id} 进程已死亡，清理")
                    self._cleanup_persistent_slot(slot)
            elif slot.status == "busy":
                if slot.process is not None and slot.process.poll() is not None:
                    logger.warning(f"[SC-Pool] 常驻槽位{slot.slot_id} busy 但进程已死亡，清理复用")
                    self._cleanup_persistent_slot(slot)
                    return slot

        active_slots = sum(
            1 for s in self._persistent_slots.values()
            if s.status in ("starting", "ready", "busy")
            and (s.process is None or s.process.poll() is None)
        )
        if active_slots >= self.MAX_SLOTS:
            logger.warning(f"[SC-Pool] 常驻槽位已满 ({active_slots}/{self.MAX_SLOTS})")
            return None

        slot_id = len(self._persistent_slots) + 1
        slot = PersistentSlot(slot_id=slot_id, cmd_dir=self._persistent_cmd_dir)
        self._persistent_slots[slot_id] = slot

        if not self._launch_persistent_process(slot):
            return None

        return slot

    def _launch_persistent_process(self, slot: PersistentSlot) -> bool:
        """启动常驻 Bridge 进程。调用方须持有 _lock。"""
        if not self._bridge_path or not os.path.exists(self._bridge_path):
            logger.error("[SC-Pool] 常驻模式需要 Bridge (SpaceClaimBridge.exe)")
            return False

        cmd = [
            self._bridge_path,
            "--persistent",
            "--script", self._sc_script,
            "--cmddir", self._persistent_cmd_dir,
            "--slotid", str(slot.slot_id),
        ]

        slot.status = "starting"

        sc_env = os.environ.copy()
        sc_env["AUTOFLUID_SC_NOEXIT"] = "1"
        sc_env["AUTOFLUID_SC_PERSISTENT"] = "1"
        sc_env["AUTOFLUID_SC_CMD_DIR"] = self._persistent_cmd_dir
        sc_env["AUTOFLUID_SC_SLOT_ID"] = str(slot.slot_id)

        logger.info(f"[SC-Pool] 启动常驻 Bridge: 槽位{slot.slot_id}")
        logger.debug(f"[SC-Pool]   命令: {' '.join(cmd)}")

        self._cleanup_ipc_files(slot.slot_id)

        try:
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation_flags, env=sc_env,
            )
            slot.process = process
            slot.pid = process.pid
            slot.started_at = time.time()
            logger.info(f"[SC-Pool] 常驻 Bridge PID={process.pid}")

            ready_file = os.path.join(self._persistent_cmd_dir, f"sc_ready_{slot.slot_id}.json")
            ready_timeout = ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)
            deadline = time.time() + ready_timeout

            self._lock.release()
            try:
                while time.time() < deadline:
                    if process.poll() is not None:
                        logger.error(f"[SC-Pool] 常驻 Bridge 槽位{slot.slot_id} 启动失败 (exit={process.returncode})")
                        with self._lock:
                            self._cleanup_persistent_slot(slot)
                        return False
                    if os.path.exists(ready_file):
                        logger.info(f"[SC-Pool] 常驻槽位{slot.slot_id} 就绪")
                        with self._lock:
                            slot.status = "ready"
                        return True
                    time.sleep(2)
            finally:
                self._lock.acquire()

            logger.error(f"[SC-Pool] 常驻槽位{slot.slot_id} 就绪超时 ({ready_timeout}s)")
            self._cleanup_persistent_slot(slot)
            return False

        except OSError as e:
            logger.error(f"[SC-Pool] 常驻 Bridge 启动失败: {e}")
            self._cleanup_persistent_slot(slot)
            return False

    # ==================================================================
    # 命令发送与结果等待
    # ==================================================================

    def _send_persistent_command(self, slot: PersistentSlot, config_name: int,
                                 paused_event: Optional[threading.Event],
                                 stopped_event: Optional[threading.Event]) -> bool:
        """向常驻进程发送命令并等待结果。"""
        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]
        scdoc_name = get_step_filename("SC", config_name)

        if not scdoc_name:
            logger.error(f"[SC-Pool] 无法生成构型{config_name} SCDOC 文件名")
            return False

        scdoc_file = os.path.join(scdoc_dir, scdoc_name)
        os.makedirs(scdoc_dir, exist_ok=True)

        cmd_file = os.path.join(self._persistent_cmd_dir, f"sc_cmd_{slot.slot_id}.json")
        result_file = os.path.join(self._persistent_cmd_dir, f"sc_result_{slot.slot_id}.json")

        cmd_data = {"config": config_name, "stepdir": step_dir, "scdocdir": scdoc_dir}
        try:
            with open(cmd_file, "w") as f:
                json.dump(cmd_data, f)
        except OSError as e:
            logger.error(f"[SC-Pool] 写入命令文件失败: {e}")
            return False

        logger.info(f"[SC-Pool] 构型{config_name} 命令已发送 (槽位{slot.slot_id})")

        timeout = ENGINE_CONFIG["sc_timeout"]
        deadline = time.time() + timeout
        poll_interval = OPERATION_TIMEOUTS["sc_poll_interval"]

        while True:
            if slot.process is not None and slot.process.poll() is not None:
                logger.error(f"[SC-Pool] 常驻 Bridge 槽位{slot.slot_id} 意外退出 (exit={slot.process.returncode})")
                return False

            if paused_event is not None and paused_event.is_set():
                logger.info(f"[SC-Pool] 构型{config_name} 因暂停取消")
                self._cleanup_ipc_files(slot.slot_id)
                return False

            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[SC-Pool] 构型{config_name} 因停止取消")
                self._cleanup_ipc_files(slot.slot_id)
                return False

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
                    if success:
                        if os.path.exists(scdoc_file):
                            file_size = os.path.getsize(scdoc_file)
                            logger.info(f"[SC-Pool] OK 构型{config_name} SCDOC: {os.path.basename(scdoc_file)} ({file_size} bytes)")
                        return True
                    else:
                        logger.error(f"[SC-Pool] FAIL 构型{config_name} 转换失败: {message}")
                        return False
                except (ValueError, IOError, OSError) as e:
                    logger.error(f"[SC-Pool] 读取结果文件异常: {e}")
                    try:
                        os.remove(result_file)
                    except OSError:
                        pass
                    return False

            if time.time() >= deadline:
                logger.error(f"[SC-Pool] 构型{config_name} 超时 ({timeout}s)")
                self._cleanup_ipc_files(slot.slot_id)
                return False

            time.sleep(poll_interval)

    # ==================================================================
    # 槽位生命周期
    # ==================================================================

    def _cleanup_ipc_files(self, slot_id: int) -> None:
        for suffix in [f"sc_cmd_{slot_id}.json", f"sc_result_{slot_id}.json",
                       f"sc_result_{slot_id}.json.tmp"]:
            path = os.path.join(self._persistent_cmd_dir, suffix)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _cleanup_persistent_slot(self, slot: PersistentSlot) -> None:
        """清理常驻槽位（终止进程、清理文件）。调用方须持有 _lock。"""
        if slot.process is not None:
            try:
                slot.process.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                slot.process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                pass
            slot.process = None
        slot.pid = None
        slot.status = "idle"
        slot.current_config = None
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
                slot.process.kill()
                slot.process.communicate(timeout=5)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                pass
        slot.process = None
        slot.pid = None
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
                "max_slots": self.MAX_SLOTS,
                "ready": ready_count,
                "busy": busy_count,
                "slots": {
                    sid: {"slot_id": s.slot_id, "status": s.status,
                          "pid": s.pid, "current_config": s.current_config}
                    for sid, s in self._persistent_slots.items()
                },
            }
