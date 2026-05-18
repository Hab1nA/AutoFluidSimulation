import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List

from engine.config import LOCAL_PATHS, ENGINE_CONFIG, OPERATION_TIMEOUTS, get_step_filename
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
    """SpaceClaim 进程池，管理最多 MAX_SLOTS 个并发 SC 实例。

    设计说明：
    - 控制事件（paused_event / stopped_event）通过方法参数传递，
      而非实例属性存储。这避免了与 TaskRunner.set_control_events
      的状态同步问题——SCProcessPool 是无状态的工具类，每次调用
      都从调用方获取最新的控制事件。
    """
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

        # ---- 常驻模式 ----
        self._process_mode = ENGINE_CONFIG.get("sc_process_mode", "oneshot")
        self._persistent_slots: Dict[int, PersistentSlot] = {}
        self._persistent_cmd_dir = os.path.join(self._data_dir, "sc_ipc")
        os.makedirs(self._persistent_cmd_dir, exist_ok=True)

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
                    f"[SC-Pool] 已加载 {len(self._slots)} 个槽位 "
                    f"(max={self.MAX_SLOTS})"
                )
            except FileNotFoundError:
                # 防御：os.path.exists 可能因 mock/TOCTOU 返回 True 但文件实际不存在
                logger.info("[SC-Pool] 池文件不存在，初始化空池")
                self._slots = {}
                self._next_slot_id = 1
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"[SC-Pool] 池文件损坏，使用空池: {e}")
                self._slots = {}
                self._next_slot_id = 1
        else:
            logger.info("[SC-Pool] 池文件不存在，初始化空池")

    def _save_pool(self):
        try:
            data = {
                "slots": [asdict(s) for s in self._slots.values()],
                "next_slot_id": self._next_slot_id,
            }
            with open(self._pool_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as e:
            logger.error(f"[SC-Pool] 写入池文件失败: {e}")

    def _check_process_alive(self, slot: SCSlot):
        if slot.pid is None or slot.status == "idle":
            return
        try:
            os.kill(slot.pid, 0)
        except (OSError, ProcessLookupError):
            logger.info(
                f"[SC-Pool] 槽位{slot.slot_id} PID={slot.pid} "
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
            logger.info("[SC-Pool] 执行全量 SpaceClaim 进程清理...")
            # 关闭常驻模式槽位
            for slot in self._persistent_slots.values():
                self._shutdown_persistent_slot(slot)
            # 关闭传统模式
            self._kill_all_sc_processes()
            for slot in self._slots.values():
                slot.pid = None
                slot.status = "idle"
                slot.config_name = None
                slot.completed_at = time.time()
            self._save_pool()
            logger.info("[SC-Pool] 全量清理完成，所有槽位重置为 idle")

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
            self._wait_queue.clear()
            self._slots.clear()
            self._next_slot_id = 1
            # 清理常驻模式
            for slot in self._persistent_slots.values():
                self._cleanup_persistent_slot(slot)
            self._persistent_slots.clear()
            self._save_pool()
            logger.info("[SC-Pool] 已重置：清理标志/等待队列/槽位/常驻进程全部归零")

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
                f"[SC-Pool] 构型{config_name} 加入等待队列 "
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
            f"[SC-Pool] 槽位{slot.slot_id} 分配给构型{config_name}"
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
            f"[SC-Pool] 创建新槽位{slot.slot_id} → 构型{config_name} "
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
                logger.info(f"[SC-Pool] 构型{config_name} 因引擎停止取消等待")
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
                logger.warning(f"[SC-Pool] 释放失败: 槽位{slot_id} 不存在")
                return

            config_name = slot.config_name
            slot.pid = None
            slot.status = "idle"
            slot.config_name = None
            slot.completed_at = time.time()
            self._save_pool()

            logger.info(
                f"[SC-Pool] 槽位{slot_id} 已释放 (构型{config_name})"
            )

            if self._wait_queue:
                next_config = self._wait_queue[0]
                logger.info(
                    f"[SC-Pool] 等待队列首位 构型{next_config} "
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
                logger.info(f"[SC-Pool] 槽位{slot_id} PID={pid}")

    # ==================================================================
    # 执行入口：一体化 launch + monitor + release
    # ==================================================================

    def run_config(self, config_name: int,
                   paused_event: Optional[threading.Event] = None,
                   stopped_event: Optional[threading.Event] = None) -> bool:
        if self._process_mode == "pooled":
            return self._execute_in_pooled_mode(config_name, paused_event, stopped_event)

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
            logger.error(f"[SC-Pool] 无法生成构型{config_name} SCDOC 文件名")
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
            f"[SC-Pool] 启动 Bridge: 构型{config_name} 槽位{slot_id}"
        )
        logger.debug(f"[SC-Pool]   命令: {' '.join(cmd)}")

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
            poll_interval = OPERATION_TIMEOUTS["sc_poll_interval"]

            while True:
                retcode = process.poll()
                if retcode is not None:
                    if retcode != 0:
                        logger.error(
                            f"[SC-Pool] Bridge 失败 构型{config_name} "
                            f"(exit={retcode})"
                        )
                        return False
                    break

                if paused_event is not None and paused_event.is_set():
                    logger.info(
                        f"[SC-Pool] 构型{config_name} 因暂停被终止"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                if stopped_event is not None and stopped_event.is_set():
                    logger.info(
                        f"[SC-Pool] 构型{config_name} 因停止被终止"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                if time.time() >= deadline:
                    logger.error(
                        f"[SC-Pool] 构型{config_name} Bridge 超时 ({timeout}s)"
                    )
                    self._terminate_bridge_and_sc(process, config_name)
                    return False

                time.sleep(poll_interval)

            if os.path.exists(scdoc_file):
                file_size = os.path.getsize(scdoc_file)
                logger.info(
                    f"[SC-Pool] ✓ 构型{config_name} SCDOC: "
                    f"{os.path.basename(scdoc_file)} ({file_size} bytes)"
                )
                return True
            else:
                logger.error(
                    f"[SC-Pool] ✗ 构型{config_name} SCDOC 未生成"
                )
                return False

        except OSError as e:
            logger.error(f"[SC-Pool] 构型{config_name} IO错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[SC-Pool] 构型{config_name} 未知错误: {e}", exc_info=True)
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
        logger.info(f"[SC-Pool] 已终止 构型{config_name} 的 Bridge 及关联 SC 进程")

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

        logger.error("[SC-Pool] 无可用的 SC 调用方式 (Bridge/SC exe 均缺失)")
        return None

    def _build_persistent_command(self, slot_id: int) -> Optional[List[str]]:
        """构建常驻模式的 Bridge 启动命令。"""
        if self._bridge_path and os.path.exists(self._bridge_path):
            return [
                self._bridge_path,
                "--persistent",
                "--script", self._sc_script,
                "--cmddir", self._persistent_cmd_dir,
                "--slotid", str(slot_id),
            ]

        logger.error("[SC-Pool] 常驻模式需要 Bridge (SpaceClaimBridge.exe)")
        return None

    # ==================================================================
    # 常驻模式 (pooled)
    # ==================================================================

    def _execute_in_pooled_mode(self, config_name: int,
                                paused_event: Optional[threading.Event],
                                stopped_event: Optional[threading.Event]) -> bool:
        """常驻模式：复用已运行的 SpaceClaim 进程。"""
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
                # 验证 slot 仍在字典中（可能因进程崩溃被其他线程清理）
                if slot.slot_id in self._persistent_slots:
                    slot.status = "ready"
                    slot.current_config = None

    def _get_or_create_persistent_slot(self) -> Optional[PersistentSlot]:
        """获取空闲的常驻槽位，若无则创建新槽位。调用方须持有 _lock。"""
        # 查找可用槽位（ready 或 busy 但进程已死亡）
        for slot in self._persistent_slots.values():
            if slot.status == "ready":
                # 检查进程是否仍然存活
                if slot.process is not None and slot.process.poll() is None:
                    return slot
                else:
                    logger.warning(
                        f"[SC-Pool] 常驻槽位{slot.slot_id} 进程已死亡，清理"
                    )
                    self._cleanup_persistent_slot(slot)
            elif slot.status == "busy":
                # busy 状态但进程已死亡 → 清理并复用
                if slot.process is not None and slot.process.poll() is not None:
                    logger.warning(
                        f"[SC-Pool] 常驻槽位{slot.slot_id} busy 但进程已死亡，清理复用"
                    )
                    self._cleanup_persistent_slot(slot)
                    return slot

        # 检查是否达到最大槽位数
        active_slots = sum(
            1 for s in self._persistent_slots.values()
            if s.status in ("starting", "ready", "busy")
            and (s.process is None or s.process.poll() is None)
        )
        if active_slots >= self.MAX_SLOTS:
            logger.warning(f"[SC-Pool] 常驻槽位已满 ({active_slots}/{self.MAX_SLOTS})")
            return None

        # 创建新槽位
        slot_id = len(self._persistent_slots) + 1
        slot = PersistentSlot(slot_id=slot_id, cmd_dir=self._persistent_cmd_dir)
        self._persistent_slots[slot_id] = slot

        if not self._launch_persistent_process(slot):
            return None

        return slot

    def _launch_persistent_process(self, slot: PersistentSlot) -> bool:
        """启动常驻 Bridge 进程。调用方须持有 _lock。"""
        cmd = self._build_persistent_command(slot.slot_id)
        if cmd is None:
            return False

        slot.status = "starting"

        sc_env = os.environ.copy()
        sc_env["AUTOFLUID_SC_NOEXIT"] = "1"
        sc_env["AUTOFLUID_SC_PERSISTENT"] = "1"
        sc_env["AUTOFLUID_SC_CMD_DIR"] = self._persistent_cmd_dir
        sc_env["AUTOFLUID_SC_SLOT_ID"] = str(slot.slot_id)

        logger.info(f"[SC-Pool] 启动常驻 Bridge: 槽位{slot.slot_id}")
        logger.debug(f"[SC-Pool]   命令: {' '.join(cmd)}")

        # 清理旧的 IPC 文件
        self._cleanup_ipc_files(slot.slot_id)

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
            slot.process = process
            slot.pid = process.pid
            slot.started_at = time.time()
            logger.info(f"[SC-Pool] 常驻 Bridge PID={process.pid}")

            # 等待就绪标志文件
            ready_file = os.path.join(
                self._persistent_cmd_dir,
                f"sc_ready_{slot.slot_id}.json",
            )
            ready_timeout = ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)
            deadline = time.time() + ready_timeout

            # 释放锁等待就绪（避免阻塞其他操作）
            self._lock.release()
            try:
                while time.time() < deadline:
                    if process.poll() is not None:
                        logger.error(
                            f"[SC-Pool] 常驻 Bridge 槽位{slot.slot_id} "
                            f"启动失败 (exit={process.returncode})"
                        )
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

            logger.error(
                f"[SC-Pool] 常驻槽位{slot.slot_id} 就绪超时 ({ready_timeout}s)"
            )
            self._cleanup_persistent_slot(slot)
            return False

        except OSError as e:
            logger.error(f"[SC-Pool] 常驻 Bridge 启动失败: {e}")
            self._cleanup_persistent_slot(slot)
            return False

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

        # 写入命令文件
        cmd_file = os.path.join(self._persistent_cmd_dir, f"sc_cmd_{slot.slot_id}.json")
        result_file = os.path.join(self._persistent_cmd_dir, f"sc_result_{slot.slot_id}.json")

        cmd_data = {
            "config": config_name,
            "stepdir": step_dir,
            "scdocdir": scdoc_dir,
        }

        try:
            with open(cmd_file, "w") as f:
                json.dump(cmd_data, f)
        except OSError as e:
            logger.error(f"[SC-Pool] 写入命令文件失败: {e}")
            return False

        logger.info(f"[SC-Pool] 常驻模式: 构型{config_name} 命令已发送 (槽位{slot.slot_id})")

        # 轮询结果文件
        timeout = ENGINE_CONFIG["sc_timeout"]
        deadline = time.time() + timeout
        poll_interval = OPERATION_TIMEOUTS["sc_poll_interval"]

        while True:
            # 检查进程是否仍然存活
            if slot.process is not None and slot.process.poll() is not None:
                logger.error(
                    f"[SC-Pool] 常驻 Bridge 槽位{slot.slot_id} "
                    f"意外退出 (exit={slot.process.returncode})"
                )
                return False

            # 暂停/停止检查
            if paused_event is not None and paused_event.is_set():
                logger.info(f"[SC-Pool] 构型{config_name} 因暂停取消")
                self._cleanup_ipc_files(slot.slot_id)
                return False

            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[SC-Pool] 构型{config_name} 因停止取消")
                self._cleanup_ipc_files(slot.slot_id)
                return False

            # 检查结果文件
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r") as f:
                        result_data = json.load(f)

                    success = result_data.get("success", False)
                    message = result_data.get("message", "")

                    # 清理结果文件
                    try:
                        os.remove(result_file)
                    except OSError:
                        pass

                    if success:
                        if os.path.exists(scdoc_file):
                            file_size = os.path.getsize(scdoc_file)
                            logger.info(
                                f"[SC-Pool] ✓ 构型{config_name} SCDOC: "
                                f"{os.path.basename(scdoc_file)} ({file_size} bytes)"
                            )
                        return True
                    else:
                        logger.error(
                            f"[SC-Pool] ✗ 构型{config_name} 转换失败: {message}"
                        )
                        return False

                except (ValueError, IOError, OSError) as e:
                    logger.error(f"[SC-Pool] 读取结果文件异常: {e}")
                    try:
                        os.remove(result_file)
                    except OSError:
                        pass
                    return False

            # 超时检查
            if time.time() >= deadline:
                logger.error(
                    f"[SC-Pool] 构型{config_name} 常驻模式超时 ({timeout}s)"
                )
                self._cleanup_ipc_files(slot.slot_id)
                return False

            time.sleep(poll_interval)

    def _cleanup_ipc_files(self, slot_id: int) -> None:
        """清理指定槽位的 IPC 文件。"""
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

        # 尝试发送 quit 命令
        quit_file = os.path.join(self._persistent_cmd_dir, f"sc_cmd_{slot.slot_id}.json")
        try:
            with open(quit_file, "w") as f:
                json.dump({"command": "quit"}, f)
        except OSError:
            pass

        # 等待进程退出
        try:
            slot.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # 兜底：强制终止
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
