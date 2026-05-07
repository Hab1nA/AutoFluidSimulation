"""
===============================================================================
文件监控模块 (File Monitor)
基于 watchdog 库实现的 STEP 文件目录监控。
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
from typing import Callable, Optional, Set

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
        history[:] = [(t, s) for t, s in history if t >= cutoff]

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

    设计为轮询模式（兼容性好，不依赖 watchdog 的 Native 文件系统事件），
    同时也支持 watchdog 事件驱动的混合模式。
    """

    _SW_STEP_PATTERN = STEP_FILE_PATTERNS.get("SW", "model_gen4.SLDPRT_{config}.step")

    @staticmethod
    def _compile_config_regex(pattern: str) -> Optional[re.Pattern]:
        """
        将 STEP_FILE_PATTERNS["SW"] 形式的模板编译成正则表达式。

        约束：模板中必须包含 `{config}` 占位符，否则无法解析构型号。
        """
        if "{config}" not in pattern:
            return None
        escaped = re.escape(pattern)
        escaped = escaped.replace(re.escape("{config}"), r"(?P<config>\d+)")
        return re.compile(rf"^{escaped}$", flags=re.IGNORECASE)

    _FILENAME_REGEX: Optional[re.Pattern] = None

    @classmethod
    def _get_filename_regex(cls) -> Optional[re.Pattern]:
        """获取或延迟编译文件名匹配正则（线程安全：幂等操作）。"""
        if cls._FILENAME_REGEX is None:
            sw_pattern = STEP_FILE_PATTERNS.get("SW", "model_gen4.SLDPRT_{config}.step")
            cls._FILENAME_REGEX = cls._compile_config_regex(sw_pattern)
        return cls._FILENAME_REGEX

    def __init__(self, step_dir: str = None,
                 on_file_ready: Callable[[int, str], None] = None):
        """
        初始化文件监控器。

        Args:
            step_dir: STEP 文件目录路径
            on_file_ready: 文件就绪回调，参数为 (构型名称, 文件路径)
        """
        self.step_dir = step_dir or LOCAL_PATHS["step_dir"]
        self.on_file_ready = on_file_ready
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._detector = FileStableDetector(
            stable_time=2.0,
            check_interval=ENGINE_CONFIG["watchdog_interval"]
        )
        # 已处理的文件集合（避免重复处理）
        self._processed_files: Set[str] = set()
        # 已发现的文件集合
        self._known_files: Set[str] = set()

        # 确保正则已编译（__init__ 时 _SW_STEP_PATTERN 已从 config 加载）
        self._get_filename_regex()

    # ------------------------------------------------------------------
    # 文件名解析
    # ------------------------------------------------------------------

    @classmethod
    def parse_config_name(cls, filename: str) -> Optional[int]:
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
        """停止文件监控。"""
        self._running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        # 清理检测器内部历史记录，防止内存泄漏
        self._detector._history.clear()
        self._detector._first_seen.clear()
        logger.info("STEP 文件监控已停止")

    # ------------------------------------------------------------------
    # 监控主循环（轮询模式）
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        """
        监控主循环。

        定期扫描 STEP 目录，检测新文件和文件写入完成事件。
        """
        logger.info("文件监控循环开始")

        while self._running:
            try:
                self._scan_directory()
            except OSError as e:
                logger.error(f"文件扫描异常: {e}")

            time.sleep(ENGINE_CONFIG["watchdog_interval"])

        logger.info("文件监控循环结束")

    def _scan_directory(self):
        """扫描 STEP 目录，检测文件变化。"""
        if not os.path.isdir(self.step_dir):
            logger.debug(f"STEP 目录不存在: {self.step_dir}")
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
        if not os.path.isdir(self.step_dir):
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

    # ------------------------------------------------------------------
    # 已处理文件查询
    # ------------------------------------------------------------------

    def is_processed(self, filename: str) -> bool:
        """检查文件是否已被处理。"""
        return filename in self._processed_files

    def get_pending_configs(self) -> list:
        """获取所有未被处理的构型列表（扫描 STEP 目录）。"""
        pending = []
        if not os.path.isdir(self.step_dir):
            return pending
        try:
            for filename in os.listdir(self.step_dir):
                if filename in self._processed_files:
                    continue
                filepath = os.path.join(self.step_dir, filename)
                if not os.path.isfile(filepath):
                    continue
                config_name = self.parse_config_name(filename)
                if config_name is not None:
                    pending.append((config_name, filepath))
        except OSError as e:
            logger.warning(f"扫描待处理文件时出错: {e}")
        return pending
