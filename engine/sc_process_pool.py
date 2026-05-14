import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List

from engine.config import LOCAL_PATHS, ENGINE_CONFIG, get_step_filename, STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED
from utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class SCSlot:
    slot_id: int
    pid: Optional[int] = None
    status: str = "idle"
    config_name: Optional[int] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class SCProcessPool:
    MAX_SLOTS = 3

    def __init__(self):
        self._data_dir = LOCAL_PATHS.get("data_dir", "data")
        os.makedirs(self._data_dir, exist_ok=True)
        self._pool_file = os.path.join(self._data_dir, "sc_process_pool.json")
        self._lock = threading.RLock()
        self._wait_cv = threading.Condition(self._lock)
        self._wait_queue: List[int] = []
        self._first_cleanup_done = False
        self._final_cleanup_done = False
        self._slots: Dict[int, SCSlot] = {}
        self._next_slot_id = 1

        self._bridge_path = LOCAL_PATHS.get("sc_bridge", "")
        self._sc_exe = LOCAL_PATHS.get("sc_exe", "")
        self._sc_script = LOCAL_PATHS.get("sc_script", "")

        self._load_pool()

    def _load_pool(self):
        if os.path.exists(self._pool_file):
            try:
                with open(self._pool_file, "r") as f:
                    data = json.load(f)
                for slot_data in data.get("slots", []):
                    slot = SCSlot(**slot_data)
                    self._check_process_alive(slot)
                    self._slots[slot.slot_id] = slot
                self._next_slot_id = data.get("next_slot_id", 1)
                logger.info(
                    f"[SCPool] 已加载 {len(self._slots)} 个槽位 "
                    f"(max={self.MAX_SLOTS})"
                )
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"[SCPool] 池文件损坏，使用空池: {e}")
                self._slots = {}
                self._next_slot_id = 1
        else:
            logger.info("[SCPool] 池文件不存在，初始化空池")

    def _save_pool(self):
        try:
            data = {
                "slots": [asdict(s) for s in self._slots.values()],
                "next_slot_id": self._next_slot_id,
            }
            with open(self._pool_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as e:
            logger.error(f"[SCPool] 写入池文件失败: {e}")

    def _check_process_alive(self, slot: SCSlot):
        if slot.pid is None or slot.status == "idle":
            return
        try:
            os.kill(slot.pid, 0)
        except (OSError, ProcessLookupError):
            logger.info(
                f"[SCPool] 槽位{slot.slot_id} PID={slot.pid} "
                f"已不存在，重置为 idle"
            )
            slot.pid = None
            slot.status = "idle"
            slot.config_name = None
            self._save_pool()

    # ==================================================================
    # 全量清理（首次/末次）
    # ==================================================================

    def shutdown_all(self):
        with self._lock:
            logger.info("[SCPool] 执行全量 SpaceClaim 进程清理...")
            self._kill_all_sc_processes()
            for slot in self._slots.values():
                slot.pid = None
                slot.status = "idle"
                slot.config_name = None
                slot.completed_at = time.time()
            self._save_pool()
            logger.info("[SCPool] 全量清理完成，所有槽位重置为 idle")

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
            logger.warning(f"[SCPool] taskkill 异常: {e}")

    def do_first_cleanup(self):
        with self._lock:
            if not self._first_cleanup_done:
                logger.info("[SCPool] === 首次全体 SC 进程清理（进入 SC 阶段前）===")
                self.shutdown_all()
                self._first_cleanup_done = True

    def do_final_cleanup(self):
        with self._lock:
            if not self._final_cleanup_done:
                logger.info("[SCPool] === 末次全体 SC 进程清理（SC 阶段全部完成后）===")
                self.shutdown_all()
                self._final_cleanup_done = True

    def reset(self):
        with self._lock:
            self._first_cleanup_done = False
            self._final_cleanup_done = False
            self._wait_queue.clear()
            self._slots.clear()
            self._next_slot_id = 1
            self._save_pool()
            logger.info("[SCPool] 已重置：清理标志/等待队列/槽位全部归零")

    # ==================================================================
    # 槽位获取与释放
    # ==================================================================

    def acquire(self, config_name: int,
                paused_event: Optional[threading.Event] = None,
                stopped_event: Optional[threading.Event] = None) -> Optional[int]:
        with self._lock:
            self._cleanup_dead_slots()

            idle_slot = self._find_idle_slot()
            if idle_slot is not None:
                return self._assign_slot(idle_slot, config_name)

            if len(self._slots) < self.MAX_SLOTS:
                return self._create_and_assign_slot(config_name)

            self._wait_queue.append(config_name)
            logger.info(
                f"[SCPool] 构型{config_name} 加入等待队列 "
                f"(当前等待: {len(self._wait_queue)}人, "
                f"活跃槽位: {len(self._slots)}/{self.MAX_SLOTS})"
            )

        return self._wait_for_slot(config_name, paused_event, stopped_event)

    def _cleanup_dead_slots(self):
        for slot in list(self._slots.values()):
            self._check_process_alive(slot)

    def _find_idle_slot(self) -> Optional[SCSlot]:
        for slot in self._slots.values():
            if slot.status == "idle":
                return slot
        return None

    def _assign_slot(self, slot: SCSlot, config_name: int) -> int:
        slot.status = "in_use"
        slot.config_name = config_name
        self._save_pool()
        logger.info(
            f"[SCPool] 槽位{slot.slot_id} 分配给构型{config_name}"
        )
        return slot.slot_id

    def _create_and_assign_slot(self, config_name: int) -> int:
        slot = SCSlot(
            slot_id=self._next_slot_id,
            status="in_use",
            config_name=config_name,
        )
        self._slots[slot.slot_id] = slot
        self._next_slot_id += 1
        self._save_pool()
        logger.info(
            f"[SCPool] 创建新槽位{slot.slot_id} → 构型{config_name} "
            f"(池:{len(self._slots)}/{self.MAX_SLOTS})"
        )
        return slot.slot_id

    def _wait_for_slot(self, config_name: int,
                       paused_event: Optional[threading.Event],
                       stopped_event: Optional[threading.Event]) -> Optional[int]:
        while True:
            if stopped_event is not None and stopped_event.is_set():
                with self._lock:
                    if config_name in self._wait_queue:
                        self._wait_queue.remove(config_name)
                        self._save_pool()
                logger.info(f"[SCPool] 构型{config_name} 因引擎停止取消等待")
                return None

            if paused_event is not None and paused_event.is_set():
                time.sleep(1)
                continue

            with self._lock:
                self._cleanup_dead_slots()
                idle_slot = self._find_idle_slot()
                if idle_slot is not None and config_name in self._wait_queue:
                    self._wait_queue.remove(config_name)
                    return self._assign_slot(idle_slot, config_name)

            with self._wait_cv:
                self._wait_cv.wait(timeout=5.0)

    def release(self, slot_id: int):
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                logger.warning(f"[SCPool] 释放失败: 槽位{slot_id} 不存在")
                return

            config_name = slot.config_name
            slot.pid = None
            slot.status = "idle"
            slot.config_name = None
            slot.completed_at = time.time()
            self._save_pool()

            logger.info(
                f"[SCPool] 槽位{slot_id} 已释放 (构型{config_name})"
            )

            if self._wait_queue:
                next_config = self._wait_queue[0]
                logger.info(
                    f"[SCPool] 等待队列首位 构型{next_config} "
                    f"将被唤醒 (剩余等待: {len(self._wait_queue)-1}人)"
                )

        with self._wait_cv:
            self._wait_cv.notify_all()

    def update_pid(self, slot_id: int, pid: int):
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot:
                slot.pid = pid
                slot.started_at = time.time()
                self._save_pool()
                logger.info(f"[SCPool] 槽位{slot_id} PID={pid}")

    # ==================================================================
    # 执行入口：一体化 launch + monitor + release
    # ==================================================================

    def run_config(self, config_name: int,
                   paused_event: Optional[threading.Event] = None,
                   stopped_event: Optional[threading.Event] = None) -> bool:
        slot_id = self.acquire(config_name, paused_event, stopped_event)
        if slot_id is None:
            return False

        try:
            return self._execute_in_slot(slot_id, config_name, paused_event, stopped_event)
        finally:
            self.release(slot_id)

    def _execute_in_slot(self, slot_id: int, config_name: int,
                         paused_event: Optional[threading.Event],
                         stopped_event: Optional[threading.Event]) -> bool:
        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]

        scdoc_name = get_step_filename("SC", config_name)
        if not scdoc_name:
            logger.error(f"[SCPool] 无法生成构型{config_name} SCDOC 文件名")
            return False
        scdoc_file = os.path.join(scdoc_dir, scdoc_name)

        os.makedirs(scdoc_dir, exist_ok=True)

        cmd = self._build_command(config_name, step_dir, scdoc_dir)
        if cmd is None:
            return False

        sc_env = os.environ.copy()
        sc_env["AUTOFLUID_SC_NOEXIT"] = "1"
        sc_env["AUTOFLUID_SC_CONFIG"] = str(config_name)
        sc_env["AUTOFLUID_SC_STEP_DIR"] = step_dir
        sc_env["AUTOFLUID_SC_SCDOC_DIR"] = scdoc_dir

        logger.info(
            f"[SCPool] 启动 Bridge: 构型{config_name} 槽位{slot_id}"
        )
        logger.debug(f"[SCPool]   命令: {' '.join(cmd)}")

        try:
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                env=sc_env,
            )

            if process.pid:
                self.update_pid(slot_id, process.pid)

            timeout = ENGINE_CONFIG["sc_timeout"]
            deadline = time.time() + timeout
            poll_interval = 2.0

            while True:
                retcode = process.poll()
                if retcode is not None:
                    if retcode != 0:
                        logger.error(
                            f"[SCPool] Bridge 失败 构型{config_name} "
                            f"(exit={retcode})"
                        )
                        return False
                    break

                if paused_event is not None and paused_event.is_set():
                    logger.info(
                        f"[SCPool] 构型{config_name} 因暂停被终止"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                if stopped_event is not None and stopped_event.is_set():
                    logger.info(
                        f"[SCPool] 构型{config_name} 因停止被终止"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                if time.time() >= deadline:
                    logger.error(
                        f"[SCPool] 构型{config_name} Bridge 超时 ({timeout}s)"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                time.sleep(poll_interval)

            if os.path.exists(scdoc_file):
                file_size = os.path.getsize(scdoc_file)
                logger.info(
                    f"[SCPool] ✓ 构型{config_name} SCDOC: "
                    f"{os.path.basename(scdoc_file)} ({file_size} bytes)"
                )
                return True
            else:
                logger.error(
                    f"[SCPool] ✗ 构型{config_name} SCDOC 未生成"
                )
                return False

        except OSError as e:
            logger.error(f"[SCPool] 构型{config_name} IO错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[SCPool] 构型{config_name} 未知错误: {e}", exc_info=True)
            return False

    def _terminate_bridge_and_sc(self, bridge_process, config_name: int):
        try:
            bridge_process.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            bridge_process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            pass

        self._kill_all_sc_processes()
        logger.info(f"[SCPool] 已终止 构型{config_name} 的 Bridge 及关联 SC 进程")

    def _build_command(self, config_name: int, step_dir: str, scdoc_dir: str):
        if self._bridge_path and os.path.exists(self._bridge_path):
            return [
                self._bridge_path,
                "--script", self._sc_script,
                "--config", str(config_name),
                "--stepdir", step_dir,
                "--scdocdir", scdoc_dir,
            ]

        if self._sc_exe and os.path.exists(self._sc_exe) and self._sc_script and os.path.exists(self._sc_script):
            return [
                self._sc_exe,
                '/RunScript="{}"'.format(self._sc_script),
                "/Splash=False",
                "/Welcome=False",
                "/ExitAfterScript=True",
            ]

        logger.error("[SCPool] 无可用的 SC 调用方式 (Bridge/SC exe 均缺失)")
        return None

    # ==================================================================
    # 状态查询
    # ==================================================================

    def get_pool_status(self) -> dict:
        with self._lock:
            idle_count = sum(1 for s in self._slots.values() if s.status == "idle")
            in_use_count = sum(1 for s in self._slots.values() if s.status == "in_use")
            return {
                "total_slots": len(self._slots),
                "max_slots": self.MAX_SLOTS,
                "idle": idle_count,
                "in_use": in_use_count,
                "wait_queue_size": len(self._wait_queue),
                "slots": {sid: asdict(s) for sid, s in self._slots.items()},
                "wait_queue": list(self._wait_queue),
            }
