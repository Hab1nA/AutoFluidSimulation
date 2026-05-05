"""后台守护进程引擎。"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import paramiko

from .config import PipelineConfig
from .constants import PIPELINE_STEPS, StepName, StepStatus
from .models import ConfigRow
from .state_store import StateStore
from .utils import find_step_file, read_excel_configs, run_subprocess


@dataclass
class TaskItem:
    """任务队列项。"""

    name: str
    attempt: int = 0


class PipelineEngine:
    """后台调度引擎。"""

    def __init__(self, config: PipelineConfig, store: StateStore, logger) -> None:
        self.config = config
        self.store = store
        self.logger = logger
        self.resume_event = threading.Event()
        self.stop_event = threading.Event()
        self.solver_gate = threading.Event()

        self.sc_queue: queue.Queue[TaskItem] = queue.Queue()
        self.transfer_queue: queue.Queue[TaskItem] = queue.Queue()
        self.mesh_queue: queue.Queue[TaskItem] = queue.Queue()
        self.solver_queue: queue.Queue[TaskItem] = queue.Queue()

        self._threads: List[threading.Thread] = []
        self._monitor_thread: Optional[threading.Thread] = None
        self._sw_thread: Optional[threading.Thread] = None
        self._ipc_server = None
        self._sw_started = False

        configs = read_excel_configs(self.config.local.excel_path)
        for config in configs:
            self._sanitize_config_name(config.name)
        self.store.initialize_configs(configs)
        self._configs_cache: List[ConfigRow] = configs

    def attach_ipc_server(self, server) -> None:
        self._ipc_server = server

    def start_background(self) -> None:
        """启动监控与工作线程。"""

        if self._monitor_thread is None:
            self._monitor_thread = threading.Thread(target=self._monitor_step_dir, daemon=True)
            self._monitor_thread.start()

        self._start_workers(self.config.sc_workers, self._spaceclaim_worker)
        self._start_workers(self.config.transfer_workers, self._transfer_worker)
        self._start_workers(self.config.meshing_workers, self._meshing_worker)
        self._start_workers(self.config.solver_workers, self._solver_worker)

    def _start_workers(self, count: int, target: Callable[[], None]) -> None:
        for _ in range(count):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _wait_until_resume(self) -> bool:
        while not self.resume_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        return True

    def start_pipeline(self) -> Dict[str, str]:
        """启动或继续执行流水线。"""

        self.logger.info("流水线启动/继续执行。")
        self.resume_event.set()
        self.store.set_meta("paused", "0")
        self._start_solidworks_macro_if_needed()
        self._seed_ready_tasks()
        return {"status": "started"}

    def pause_pipeline(self) -> Dict[str, str]:
        """暂停流水线（当前任务完成后不再取新任务）。"""

        self.logger.info("流水线暂停。")
        self.resume_event.clear()
        self.store.set_meta("paused", "1")
        return {"status": "paused"}

    def shutdown(self) -> None:
        """停止所有线程。"""

        self.logger.info("后台引擎关闭。")
        self.stop_event.set()
        self.resume_event.set()
        if self._ipc_server is not None:
            threading.Thread(target=self._ipc_server.shutdown, daemon=True).start()

    def handle_command(self, payload: Dict[str, object]) -> Dict[str, object]:
        """处理 IPC 命令。"""

        command = str(payload.get("command", "")).lower()
        args = payload.get("args") or {}
        if command == "start":
            return {"ok": True, "data": self.start_pipeline()}
        if command == "pause":
            return {"ok": True, "data": self.pause_pipeline()}
        if command == "check":
            return {"ok": True, "data": self.self_check()}
        if command == "reset":
            if args.get("all"):
                return {"ok": True, "data": self.reset_all()}
            return {"ok": True, "data": self.reset_step(args)}
        if command == "clean":
            if args.get("all"):
                return {"ok": True, "data": self.clean_all()}
            return {"ok": True, "data": self.clean_step(args)}
        if command == "full_quit":
            self.shutdown()
            return {"ok": True, "message": "后台引擎已退出"}
        if command == "quit":
            return {"ok": True, "message": "前端已退出，后台继续运行"}
        return {"ok": False, "message": f"未知命令: {command}"}

    def self_check(self) -> Dict[str, object]:
        """系统自检：路径与 SSH 连通性。"""

        issues: List[str] = []
        local_paths = [
            self.config.local.solidworks_model,
            self.config.local.excel_path,
            self.config.local.macro_path,
            self.config.local.step_dir,
            self.config.local.spaceclaim_exe,
            self.config.local.sc_script,
            self.config.local.scdoc_dir,
        ]
        for path in local_paths:
            if not Path(path).exists():
                issues.append(f"本地路径不存在: {path}")

        ssh_ok = False
        try:
            self._ssh_exec("echo ok")
            ssh_ok = True
        except Exception as exc:  # pragma: no cover - SSH 异常仅记录
            issues.append(f"SSH 连接失败: {exc}")

        return {"issues": issues, "ssh_ok": ssh_ok}

    def reset_step(self, args: Dict[str, object]) -> Dict[str, str]:
        """重置指定构型/步骤。"""

        name = str(args.get("name", ""))
        step = args.get("step")
        if not name or not step:
            return {"message": "缺少构型或步骤"}
        try:
            step_name = StepName(step)
        except ValueError:
            return {"message": "步骤非法"}
        self.store.reset_steps(name, step_name)
        if step_name in (StepName.SOLIDWORKS, StepName.SPACECLAIM, StepName.TRANSFER, StepName.MESHING):
            self._clear_solver_gate()
        if step_name == StepName.SOLIDWORKS:
            self._sw_started = False
        return {"message": f"{name} 重置完成"}

    def reset_all(self) -> Dict[str, str]:
        self.store.reset_all()
        self._clear_solver_gate()
        self._sw_started = False
        return {"message": "所有构型已重置"}

    def clean_step(self, args: Dict[str, object]) -> Dict[str, str]:
        """清理指定步骤产物。"""

        step = args.get("step")
        if not step:
            return {"message": "缺少步骤"}
        try:
            step_name = StepName(step)
        except ValueError:
            return {"message": "步骤非法"}
        if step_name == StepName.SOLIDWORKS:
            self._clean_local_files(self.config.local.step_dir, ("*.step", "*.stp"))
        elif step_name == StepName.SPACECLAIM:
            self._clean_local_files(self.config.local.scdoc_dir, ("*.scdoc",))
        elif step_name == StepName.TRANSFER:
            self._clean_remote_dir(self.config.remote.remote_scdoc_dir)
        elif step_name == StepName.MESHING:
            self._run_optional_remote_clean(self.config.remote.meshing_clean_cmd)
        elif step_name == StepName.SOLVER:
            self._run_optional_remote_clean(self.config.remote.solver_clean_cmd)
        return {"message": f"{step_name.value} 清理完成"}

    def clean_all(self) -> Dict[str, str]:
        """清理全部步骤产物。"""

        self._clean_local_files(self.config.local.step_dir, ("*.step", "*.stp"))
        self._clean_local_files(self.config.local.scdoc_dir, ("*.scdoc",))
        self._clean_remote_dir(self.config.remote.remote_scdoc_dir)
        self._run_optional_remote_clean(self.config.remote.meshing_clean_cmd)
        self._run_optional_remote_clean(self.config.remote.solver_clean_cmd)
        return {"message": "全部步骤清理完成"}

    def _clean_local_files(self, directory: str, patterns: Tuple[str, ...]) -> None:
        for pattern in patterns:
            for path in Path(directory).glob(pattern):
                try:
                    path.unlink()
                except OSError as exc:
                    self.logger.warning("删除失败 %s: %s", path, exc)

    def _clean_remote_dir(self, remote_dir: str) -> None:
        safe_dir = self._sanitize_windows_path(remote_dir, "remote_dir")
        command = f'powershell -NoProfile -Command "Remove-Item -Recurse -Force \\"{safe_dir}\\\\*\\""'
        self._ssh_exec(command)

    def _run_optional_remote_clean(self, command: str) -> None:
        if not command:
            self.logger.warning("未配置远程清理命令，跳过。")
            return
        self._ssh_exec(command)

    def _clear_solver_gate(self) -> None:
        self.solver_gate.clear()
        self._drain_queue(self.solver_queue)

    def _drain_queue(self, work_queue: queue.Queue[TaskItem]) -> None:
        while True:
            try:
                work_queue.get_nowait()
                work_queue.task_done()
            except queue.Empty:
                break

    def _start_solidworks_macro_if_needed(self) -> None:
        if self._sw_started:
            return
        if all(
            self.store.get_step_status(config.name, StepName.SOLIDWORKS) == StepStatus.COMPLETED
            for config in self._configs_cache
        ):
            return
        self._sw_started = True
        for config in self._configs_cache:
            if self.store.get_step_status(config.name, StepName.SOLIDWORKS) != StepStatus.COMPLETED:
                self.store.update_step_status(config.name, StepName.SOLIDWORKS, StepStatus.RUNNING)
        self._sw_thread = threading.Thread(target=self._run_solidworks_macro, daemon=True)
        self._sw_thread.start()

    def _run_solidworks_macro(self) -> None:
        """启动 SolidWorks 并运行宏（由宏脚本一次性导出所有构型）。"""

        if sys.platform != "win32":
            self.logger.warning("当前系统非 Windows，SolidWorks 宏将被跳过。")
            return
        try:
            import win32com.client  # type: ignore

            sw_app = win32com.client.Dispatch("SldWorks.Application")
            sw_app.Visible = True
            sw_app.OpenDoc6(self.config.local.solidworks_model, 1, 0, "", 0, 0)
            sw_app.RunMacro2(self.config.local.macro_path, "", "main", 0)
        except Exception as exc:
            self.logger.error("SolidWorks 宏执行失败: %s", exc)
            for config in self._configs_cache:
                if self.store.get_step_status(config.name, StepName.SOLIDWORKS) != StepStatus.COMPLETED:
                    self.store.update_step_status(
                        config.name,
                        StepName.SOLIDWORKS,
                        StepStatus.ERROR,
                        f"SW 宏失败: {exc}",
                    )

    def _monitor_step_dir(self) -> None:
        """轮询 STEP 目录，发现新文件即触发下游流水线。"""

        extensions = ("step", "stp", "STEP", "STP")
        while not self.stop_event.is_set():
            if not self._wait_until_resume():
                continue
            for config in self._configs_cache:
                sw_status = self.store.get_step_status(config.name, StepName.SOLIDWORKS)
                step_path = find_step_file(self.config.local.step_dir, config.name, extensions)
                if step_path:
                    if sw_status != StepStatus.COMPLETED:
                        self.store.update_step_status(config.name, StepName.SOLIDWORKS, StepStatus.COMPLETED)
                    if self.store.get_step_status(config.name, StepName.SPACECLAIM) == StepStatus.WAITING:
                        self._enqueue(self.sc_queue, config.name)
            time.sleep(self.config.poll_interval)

    def _seed_ready_tasks(self) -> None:
        """根据历史状态补齐待执行任务，实现断点续传。"""

        if self.store.all_completed(StepName.MESHING):
            self.solver_gate.set()

        for config in self._configs_cache:
            if self.store.get_step_status(config.name, StepName.SOLIDWORKS) == StepStatus.COMPLETED:
                if self.store.get_step_status(config.name, StepName.SPACECLAIM) == StepStatus.WAITING:
                    if find_step_file(self.config.local.step_dir, config.name, ("step", "stp", "STEP", "STP")):
                        self._enqueue(self.sc_queue, config.name)

            if self.store.get_step_status(config.name, StepName.SPACECLAIM) == StepStatus.COMPLETED:
                if self.store.get_step_status(config.name, StepName.TRANSFER) == StepStatus.WAITING:
                    scdoc_path = Path(self.config.local.scdoc_dir) / f"{config.name}.scdoc"
                    if scdoc_path.exists():
                        self._enqueue(self.transfer_queue, config.name)

            if self.store.get_step_status(config.name, StepName.TRANSFER) == StepStatus.COMPLETED:
                if self.store.get_step_status(config.name, StepName.MESHING) == StepStatus.WAITING:
                    self._enqueue(self.mesh_queue, config.name)

            if self.solver_gate.is_set():
                if self.store.get_step_status(config.name, StepName.SOLVER) == StepStatus.WAITING:
                    self._enqueue(self.solver_queue, config.name)

    def _enqueue(self, target_queue: queue.Queue[TaskItem], name: str, attempt: int = 0) -> None:
        target_queue.put(TaskItem(name=name, attempt=attempt))

    def _spaceclaim_worker(self) -> None:
        self._worker_loop(
            self.sc_queue,
            StepName.SPACECLAIM,
            self._run_spaceclaim,
            StepName.TRANSFER,
            self.transfer_queue,
        )

    def _transfer_worker(self) -> None:
        self._worker_loop(
            self.transfer_queue,
            StepName.TRANSFER,
            self._run_transfer,
            StepName.MESHING,
            self.mesh_queue,
        )

    def _meshing_worker(self) -> None:
        self._worker_loop(self.mesh_queue, StepName.MESHING, self._run_meshing, None, None)

    def _solver_worker(self) -> None:
        self._worker_loop(self.solver_queue, StepName.SOLVER, self._run_solver, None, None, gate=self.solver_gate)

    def _worker_loop(
        self,
        work_queue: queue.Queue[TaskItem],
        step: StepName,
        handler: Callable[[str], None],
        next_step: Optional[StepName],
        next_queue: Optional[queue.Queue[TaskItem]],
        gate: Optional[threading.Event] = None,
    ) -> None:
        while not self.stop_event.is_set():
            if not self._wait_until_resume():
                continue
            if gate is not None and not gate.is_set():
                time.sleep(0.5)
                continue
            try:
                task = work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                work_queue.task_done()
                break

            status = self.store.get_step_status(task.name, step)
            if status == StepStatus.COMPLETED or status == StepStatus.ERROR:
                work_queue.task_done()
                continue

            self.store.update_step_status(task.name, step, StepStatus.RUNNING)
            try:
                handler(task.name)
            except Exception as exc:  # pragma: no cover - 运行异常记录
                self.logger.error("%s 失败: %s", step.value, exc)
                if task.attempt < self.config.max_retries:
                    self.store.update_step_status(task.name, step, StepStatus.RETRYING, str(exc))
                    self._enqueue(work_queue, task.name, task.attempt + 1)
                else:
                    self.store.update_step_status(task.name, step, StepStatus.ERROR, str(exc))
                work_queue.task_done()
                continue

            self.store.update_step_status(task.name, step, StepStatus.COMPLETED)
            if step == StepName.MESHING:
                self._check_solver_gate()
            if next_step and next_queue:
                self._enqueue(next_queue, task.name)
            work_queue.task_done()

    def _run_spaceclaim(self, name: str) -> None:
        """调用 SpaceClaim 脚本进行转换。"""

        safe_name = self._sanitize_config_name(name)
        step_path = find_step_file(self.config.local.step_dir, safe_name, ("step", "stp", "STEP", "STP"))
        if not step_path:
            raise FileNotFoundError(f"未找到 STEP 文件: {name}")
        scdoc_path = Path(self.config.local.scdoc_dir) / f"{safe_name}.scdoc"
        command = [
            self.config.local.spaceclaim_exe,
            f'/RunScript="{self.config.local.sc_script}"',
            f'/ScriptArgs="{step_path};{scdoc_path}"',
            "/Headless",
        ]
        run_subprocess(command)

    def _run_transfer(self, name: str) -> None:
        """SFTP 上传到远程工作站。"""

        safe_name = self._sanitize_config_name(name)
        scdoc_path = Path(self.config.local.scdoc_dir) / f"{safe_name}.scdoc"
        if not scdoc_path.exists():
            raise FileNotFoundError(f"SCDOC 不存在: {scdoc_path}")
        remote_path = str(Path(self.config.remote.remote_scdoc_dir) / scdoc_path.name)
        with self._open_ssh() as client:
            sftp = client.open_sftp()
            sftp.put(str(scdoc_path), remote_path)
            sftp.close()

    def _run_meshing(self, name: str) -> None:
        """触发远程网格划分脚本。"""

        command = self._build_remote_background_command(self.config.remote.meshing_script, name)
        self._ssh_exec(command)

    def _run_solver(self, name: str) -> None:
        """触发远程仿真运行脚本。"""

        command = self._build_remote_background_command(self.config.remote.solver_script, name)
        self._ssh_exec(command)

    def _check_solver_gate(self) -> None:
        if self.solver_gate.is_set():
            return
        if self.store.all_completed(StepName.MESHING):
            self.solver_gate.set()
            for config in self._configs_cache:
                if self.store.get_step_status(config.name, StepName.SOLVER) == StepStatus.WAITING:
                    self._enqueue(self.solver_queue, config.name)

    def _build_remote_background_command(self, script_path: str, name: str) -> str:
        """构建远程后台执行命令，确保 SSH 断开后进程存活。"""

        safe_script = self._sanitize_windows_path(script_path, "script_path")
        safe_name = self._sanitize_config_name(name)
        inner = f'{self.config.remote.conda_activate_cmd} && python "{safe_script}" "{safe_name}"'
        arg_list = f'/c "{inner}"'
        return (
            'powershell -NoProfile -Command '
            f'"Start-Process -FilePath cmd.exe -ArgumentList \'{arg_list}\' -WindowStyle Hidden"'
        )

    def _open_ssh(self) -> paramiko.SSHClient:
        if not self.config.remote.password:
            raise RuntimeError("未配置 SSH 密码，请设置环境变量 AUTOFLUID_SSH_PASSWORD")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.config.remote.ssh_auto_add_host_key:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=self.config.remote.host,
            port=self.config.remote.port,
            username=self.config.remote.username,
            password=self.config.remote.password,
            timeout=10,
        )
        return client

    def _ssh_exec(self, command: str) -> str:
        with self._open_ssh() as client:
            _, stdout, stderr = client.exec_command(command)
            output = stdout.read().decode("utf-8", errors="ignore")
            error = stderr.read().decode("utf-8", errors="ignore")
        if error:
            raise RuntimeError(error.strip())
        return output.strip()

    def _sanitize_windows_path(self, path: str, label: str) -> str:
        """仅允许安全的 Windows 路径字符。"""

        pattern = re.compile(r"^[A-Za-z0-9_:\-\\.\\\\ ]+$")
        if not pattern.fullmatch(path):
            raise ValueError(f"{label} 包含非法字符")
        return path

    def _sanitize_config_name(self, name: str) -> str:
        """限制构型名称，避免命令注入与路径穿越。"""

        pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
        if not pattern.fullmatch(name):
            raise ValueError("构型名称包含非法字符")
        return name
