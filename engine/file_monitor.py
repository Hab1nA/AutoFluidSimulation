from __future__ import annotations

"""
===============================================================================
文件监控模块 (File Monitor)
基于轮询实现的 STEP 文件目录监控。
当 SW 宏批量导出 .step 文件时，监控每个文件的生成完成事件，
并将完成的构型推入后续处理队列（Producer-Consumer 模式）。

工作原理：
- SW 阶段：启动一次宏，批量导出所有构型的 STEP 文件
- 监控线程：死盯 STEP 目录，检测新完成的 .step 文件
- 每检测到一个完整文件，将构型名称推入 SC 处理队列
- 通过文件大小稳定（不再增长）判断文件写入完成
===============================================================================
"""
import os
import time
import threading
import re
from typing import Callable

from engine.config import LOCAL_PATHS, ENGINE_CONFIG, STEP_FILE_PATTERNS
from utils.logger import setup_logger

logger = setup_logger(__name__)


# ============================================================================
# 文件大小稳定检测器
# ============================================================================

class FileStableDetector:
    """
    文件写入完成检测器。

    SW 在导出大型 STEP 文件时，文件会持续写入一段时间。
    通过多次采样文件大小，当文件大小在一段时间内不再变化时，
    判定文件写入完成，可以安全地被后续流程读取。
    """

    def __init__(self, stable_time: float = 2.0, check_interval: float = 0.5):
        """
        Args:
            stable_time: 文件大小需要保持稳定的时间（秒）
            check_interval: 采样间隔（秒）
        """
        self.stable_time = stable_time
        self.check_interval = check_interval
        # 记录每个文件的大小历史: {filepath: [(timestamp, size), ...]}
        self._history: dict[str, list] = {}
        # 记录每个文件首次被检测到的时间: {filepath: first_seen_timestamp}
        self._first_seen: dict[str, float] = {}

    def is_file_ready(self, filepath: str) -> bool:
        """
        检查文件是否已完成写入（大小稳定）。

        Args:
            filepath: 文件完整路径

        Returns:
            True 表示文件已完成写入，可以安全读取
        """
        if not os.path.exists(filepath):
            return False

        try:
            current_size = os.path.getsize(filepath)
        except OSError as e:
            logger.debug(f"无法获取文件大小 {filepath}: {e}")
            return False

        now = time.time()

        if filepath not in self._history:
            self._history[filepath] = []

        history = self._history[filepath]
        history.append((now, current_size))

        # 只保留最近 stable_time 秒内的记录
        cutoff = now - self.stable_time
        self._history[filepath] = [(t, s) for t, s in history if t >= cutoff]
        history = self._history[filepath]

        # 如果历史记录不足 stable_time，说明文件可能还在写入
        if len(history) < 2:
            return False

        # 检查在 stable_time 内文件大小是否保持不变
        sizes = [s for _, s in history]
        if all(s == sizes[0] for s in sizes):
            # 清理所有与此文件相关的追踪记录
            del self._history[filepath]
            self._first_seen.pop(filepath, None)
            return True

        # 防止内存泄漏：追踪文件首次被发现的时间
        # （上面 cutoff 已删除 >stable_time 的条目，history[0] 始终 <=stable_time）
        if filepath not in self._first_seen:
            self._first_seen[filepath] = now
        elif now - self._first_seen[filepath] > self.stable_time * 3:
            logger.warning(f"文件 {filepath} 长时间未稳定 (>{self.stable_time*3:.0f}s)，放弃监控")
            del self._history[filepath]
            del self._first_seen[filepath]

        return False

    def cleanup(self, filepath: str):
        """清理指定文件的检测记录。"""
        self._history.pop(filepath, None)
        self._first_seen.pop(filepath, None)


# ============================================================================
# STEP 文件监控器
# ============================================================================

class StepFileMonitor:
    """
    STEP 文件目录监控器。

    持续扫描 STEP 目录，检测新生成的 model_gen4.SLDPRT_XX.step 文件，
    当文件写入完成（大小稳定）后，通过回调函数通知调度器。

    设计为轮询模式（兼容性好，不依赖第三方文件系统事件库）。
    """

    @staticmethod
    def _compile_config_regex(pattern: str) -> re.Pattern | None:
        """
        将 STEP_FILE_PATTERNS["sw"] 形式的模板编译成正则表达式。

        约束：模板中必须包含 `{config}` 占位符，否则无法解析构型号。
        """
        if "{config}" not in pattern:
            return None
        escaped = re.escape(pattern)
        escaped = escaped.replace(re.escape("{config}"), r"(?P<config>\d+)")
        return re.compile(rf"^{escaped}$", flags=re.IGNORECASE)

    _FILENAME_REGEX: re.Pattern | None = None

    @classmethod
    def _get_filename_regex(cls) -> re.Pattern | None:
        """获取或延迟编译文件名匹配正则（线程安全：幂等操作）。"""
        if cls._FILENAME_REGEX is None:
            sw_pattern = STEP_FILE_PATTERNS.get("sw", "model_gen4.SLDPRT_{config}.step")
            cls._FILENAME_REGEX = cls._compile_config_regex(sw_pattern)
        return cls._FILENAME_REGEX

    def __init__(self, step_dir: str | None = None,
                 on_file_ready: Callable[[int, str], None] | None = None,
                 shared_paused_event: threading.Event | None = None):
        self.step_dir = step_dir or LOCAL_PATHS["step_dir"]
        self.on_file_ready = on_file_ready
        self._running = False
        self._monitor_thread: threading.Thread | None = None
        self._detector = FileStableDetector(
            stable_time=2.0,
            check_interval=ENGINE_CONFIG["watchdog_interval"]
        )
        self._processed_files: set[str] = set()
        self._known_files: set[str] = set()
        # 暂停控制：共享 Event 由调度器拥有，监控器只能读取和唤醒。
        self._owns_paused_event = shared_paused_event is None
        self._paused = shared_paused_event if shared_paused_event is not None else threading.Event()
        self._wake_event = threading.Event()
        self._need_reset = False

    @property
    def is_running(self) -> bool:
        """监控器是否正在运行。"""
        return self._running

    # ------------------------------------------------------------------
    # 文件名解析
    # ------------------------------------------------------------------

    @classmethod
    def parse_config_name(cls, filename: str) -> int | None:
        """
        从文件名中解析构型名称。

        例如: "model_gen4.SLDPRT_5.step" -> 5
              "model_gen4.SLDPRT_12.step" -> 12

        Args:
            filename: 文件名（不含路径）

        Returns:
            构型名称（整数），解析失败返回 None
        """
        regex = cls._get_filename_regex()
        if regex is None:
            return None
        match = regex.match(filename)
        if not match:
            return None
        try:
            return int(match.group("config"))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 监控生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动文件监控线程。"""
        if self._running:
            logger.warning("文件监控已在运行")
            return

        if not self._ensure_step_dir():
            return

        # 扫描目录中已存在的文件（断点续传场景）
        self._scan_existing_files()

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="StepFileMonitor"
        )
        self._monitor_thread.start()
        logger.info(f"STEP 文件监控已启动: {self.step_dir}")

    def stop(self):
        self._running = False
        self._wake_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        self.clear_tracking()
        logger.info("STEP 文件监控已停止")

    # ------------------------------------------------------------------
    # 监控主循环（轮询模式）
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        logger.info("文件监控循环开始")

        while self._running:
            # ---- 暂停期间：仅处理 _need_reset（清理状态），禁止扫描 ----
            if self._paused.is_set():
                logger.info("文件监控已暂停，等待恢复指令...")
                while self._paused.is_set() and self._running:
                    # ★ 暂停中也响应待重置请求：清理已处理文件集合和检测器状态，
                    #    使恢复后能重新扫描。但不触发 _scan_directory()，
                    #    确保不会越过暂停向 SC 队列推送构型。
                    if self._need_reset:
                        self._need_reset = False
                        self.clear_tracking()
                        self._scan_existing_files()
                        logger.info("文件监控状态已重置（暂停中，恢复后生效）")

                    self._wake_event.wait(timeout=1.0)
                    self._wake_event.clear()
                if not self._running:
                    break
                logger.info("文件监控已恢复")

            # ---- 非暂停：处理 _need_reset 后立即扫描 ----
            if self._need_reset:
                self._need_reset = False
                self.clear_tracking()
                self._scan_existing_files()
                logger.info("文件监控状态已重置，执行立即扫描")

            try:
                self._scan_directory()
            except Exception as e:
                logger.error(f"文件扫描异常 ({type(e).__name__}: {e})", exc_info=True)

            self._wake_event.wait(timeout=ENGINE_CONFIG["watchdog_interval"])
            self._wake_event.clear()

        logger.info("文件监控循环结束")

    def pause(self):
        self._paused.set()
        logger.info("STEP 文件监控已暂停")

    def clear_tracking(self) -> None:
        """立即清空文件追踪状态，供 SW 重试等同步清理场景使用。"""
        self._processed_files.clear()
        self._known_files.clear()
        self._detector._history.clear()
        self._detector._first_seen.clear()

    def request_reset(self) -> None:
        """请求监控线程清理追踪状态并重新扫描，不修改共享 pause。"""
        self._need_reset = True
        self._wake_event.set()
        logger.info("STEP 文件监控状态已标记为待重置")

    def wake(self) -> None:
        """唤醒监控线程，不修改共享 pause 或追踪状态。"""
        self._wake_event.set()

    def resume_and_reset(self):
        """兼容接口：本地模式恢复后请求重置；共享模式仅请求重置。"""
        if self._owns_paused_event:
            self._paused.clear()
        self.request_reset()

    def reset_only(self):
        """兼容接口：请求重置，不清除暂停标志。"""
        self.request_reset()

    def resume_only(self):
        """兼容接口：本地模式恢复；共享模式仅唤醒。"""
        if self._owns_paused_event:
            self._paused.clear()
        self.wake()

    def _scan_directory(self):
        """扫描 STEP 目录，检测文件变化。"""
        if not self._ensure_step_dir():
            return

        try:
            current_files = set(os.listdir(self.step_dir))
        except OSError as e:
            logger.error(f"无法读取 STEP 目录: {e}")
            return

        # 检测新文件
        new_files = current_files - self._known_files
        for filename in new_files:
            filepath = os.path.join(self.step_dir, filename)
            if os.path.isfile(filepath):
                config_name = self.parse_config_name(filename)
                if config_name is not None:
                    logger.info(f"发现新的 STEP 文件: {filename} (构型{config_name})")

        # 检测已存在的文件是否写入完成
        for filename in list(self._known_files):
            if filename in self._processed_files:
                continue
            filepath = os.path.join(self.step_dir, filename)
            if not os.path.isfile(filepath):
                continue

            config_name = self.parse_config_name(filename)
            if config_name is None:
                continue

            # 检查文件是否写入完成（大小稳定）
            if self._detector.is_file_ready(filepath):
                logger.info(f"STEP 文件写入完成: {filename} (构型{config_name})")
                self._processed_files.add(filename)
                if self.on_file_ready:
                    try:
                        self.on_file_ready(config_name, filepath)
                    except (RuntimeError, ValueError, OSError) as e:
                        logger.error(f"文件就绪回调异常: {e}")

        self._known_files = current_files

    def _scan_existing_files(self):
        """扫描目录中已存在的文件，将其加入 known_files 以便后续稳定性检测。"""
        if not self._ensure_step_dir():
            return

        try:
            existing = os.listdir(self.step_dir)
            for filename in existing:
                filepath = os.path.join(self.step_dir, filename)
                if os.path.isfile(filepath):
                    config_name = self.parse_config_name(filename)
                    if config_name is not None:
                        # 加入已知文件列表，由后续轮询检测写入完成
                        self._known_files.add(filename)
                        logger.info(f"发现已存在的 STEP 文件: {filename} (构型{config_name})")
        except OSError as e:
            logger.warning(f"扫描已存在文件时出错: {e}")

    def _ensure_step_dir(self) -> bool:
        """确保 STEP 目录存在；监控器启动早于 SW 导出时不会持续报告目录缺失。"""
        if not self.step_dir:
            logger.error("STEP 输出目录未配置")
            return False
        if os.path.isdir(self.step_dir):
            return True
        try:
            os.makedirs(self.step_dir, exist_ok=True)
            logger.info(f"已创建 STEP 目录: {self.step_dir}")
            return True
        except OSError as e:
            logger.warning(f"无法创建/访问 STEP 目录: {self.step_dir}: {e}")
            return False
