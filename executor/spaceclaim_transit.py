# -*- coding: utf-8 -*-
# ============================================================================
# SpaceClaim Transit Script — V23 兼容版本
# 功能：读取 STEP 文件 → 创建命名选择集 → 保存为 SCDOC
#
# 用法（通过本项目 Daemon 调用）：SpaceClaim.exe /RunScript=<本脚本路径> /ScriptArgs=<构型名> <STEP目录> <SCDOC输出目录>
#   args[0] = config_name   (构型编号，整数)
#   args[1] = step_dir      (STEP 文件所在目录)
#   args[2] = scdoc_dir     (SCDOC 输出目录)
#
# 手动交互式调试用法（在 SpaceClaim 控制台中输入）：
#   import sys
#   sys.argv = ["spaceclaim_transit.py", "6", r"<STEP目录>", r"<SCDOC输出目录>"]
#   execfile(r"<脚本完整路径>")
#
# 兼容版本：SpaceClaim 2023 R1 (API V23)
# 说明：args 是 SpaceClaim 在 /RunScript 模式下自动注入的全局变量，
#       包含 /ScriptArgs 中以空格分隔的参数列表。
#
# 调试：诊断日志写入 <项目根>/logs/executor/spaceclaim_transit_<PID>.log
#       每个 SpaceClaim 进程按 PID 独立记录日志，便于多进程并行时区分。
#       SpaceClaim 是 GUI 程序，print() 输出可能不会显示在终端，
#       请查看该日志文件获取脚本执行详情。
# ============================================================================
import os
import sys
import io
import codecs
import json
import time
import traceback
from datetime import datetime

# --------------------------------------------------------------------------
# 0. 尽早建立日志器（在任何可能失败的导入之前）
# --------------------------------------------------------------------------
# 与 utils.logger.setup_logger 保持一致的 API 和日志格式，
# 但不依赖 logging 模块，确保 IronPython 兼容。
# 智能定位项目根目录：
#   优先从脚本所在路径上溯（executor/ → 项目根 → logs/executor/）
#   若 __file__ 不可用（IronPython /RunScript 模式可能缺失），回退到当前工作目录

class _SpaceClaimLogger(object):
    """轻量级日志器，API 与 utils.logger 保持一致。

    日志格式: ``[timestamp] [LEVEL] [name] message``
    同时输出到文件和 stdout（print 在 SpaceClaim GUI 下可能不可见）。
    """

    _LEVEL_NAMES = {
        10: "DEBUG",
        20: "INFO",
        30: "WARNING",
        40: "ERROR",
        50: "CRITICAL",
    }

    def __init__(self, name, log_file):
        self._name = name
        self._log_file = log_file

    @property
    def log_file(self):
        """日志文件路径。"""
        return self._log_file

    def _write(self, level, msg):
        """格式化并写入一条日志（仅写文件，不 print）。

        ★ IronPython/SpaceClaim 环境中 sys.stdout 的编码不可靠，
           print() 可能在缓冲区刷新时抛出 UnicodeEncodeError，
           且异常可能绕过硬编码的 except 子句（.NET 异常映射差异）。
           因此本 logger 只写文件，不调用 print()。
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        level_name = self._LEVEL_NAMES.get(level, "INFO")
        line = "[{}] [{}] [{}] {}".format(timestamp, level_name, self._name, msg)
        try:
            # 使用 codecs.open 替代 io.open：
            # IronPython 2.7 的 io.open 不一定将 errors 参数传递到底层 StreamWriter，
            # 导致 errors="replace" 不生效，遇到 \x00 等字符时仍抛 UnicodeEncodeError。
            # codecs.open 在 Python 2 中对 errors 参数的支持更可靠。
            with codecs.open(self._log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except UnicodeEncodeError:
            # codecs.open 的 errors="replace" 在 IronPython 2.7 中仍可能失效（
            # .NET StreamWriter 编码设为 'unknown' 时），降级为 ASCII 写入。
            try:
                safe = line.encode("ascii", "replace").decode("ascii")
                with codecs.open(self._log_file, "a", encoding="ascii", errors="replace") as f:
                    f.write(safe + "\n")
            except Exception:
                pass
        except (IOError, OSError):
            pass

    def debug(self, msg):
        """记录 DEBUG 级别日志。"""
        self._write(10, msg)

    def info(self, msg):
        """记录 INFO 级别日志。"""
        self._write(20, msg)

    def warning(self, msg):
        """记录 WARNING 级别日志。"""
        self._write(30, msg)

    def error(self, msg):
        """记录 ERROR 级别日志。"""
        self._write(40, msg)

    def critical(self, msg):
        """记录 CRITICAL 级别日志。"""
        self._write(50, msg)


def _get_script_dir():
    """获取脚本所在目录，兼容 IronPython /RunScript 模式下 __file__ 缺失的情况。"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except (NameError, AttributeError):
        # __file__ 未定义（某些 IronPython 运行模式下）→ 回退到当前工作目录
        return os.getcwd()

_SCRIPT_DIR = _get_script_dir()
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)  # executor/ 的父目录即项目根
# 验证：项目根下应存在 logs/ 目录或 executor/ 目录
_env_log_dir = os.environ.get("AUTOFLUID_SC_LOG_DIR", "")
if _env_log_dir:
    _candidate_log_dir = _env_log_dir
else:
    _candidate_log_dir = os.path.join(_PROJECT_ROOT, "logs", "executor")
    if not os.path.isdir(os.path.join(_PROJECT_ROOT, "logs")):
        # 回退：在脚本所在目录下创建
        _candidate_log_dir = os.path.join(_SCRIPT_DIR, "logs", "executor")

try:
    if not os.path.isdir(_candidate_log_dir):
        os.makedirs(_candidate_log_dir)
except (OSError, IOError):
    _candidate_log_dir = os.environ.get("TEMP", _SCRIPT_DIR)

_slot_id = os.environ.get("AUTOFLUID_SC_SLOT_ID", "")
if _slot_id:
    _log_file_name = "spaceclaim_transit_slot{}_{}.log".format(_slot_id, os.getpid())
else:
    _log_file_name = "spaceclaim_transit_{}.log".format(os.getpid())
_log_path = os.path.join(_candidate_log_dir, _log_file_name)
logger = _SpaceClaimLogger("spaceclaim_transit", _log_path)

# --------------------------------------------------------------------------
# ★ 重定向 stdout：IronPython/SpaceClaim 环境中 sys.stdout 的编码不可靠，
#    print() / stdout.write() 可能在缓冲区刷新时抛出 UnicodeEncodeError。
#    将所有输出重定向到 null 设备，日志仅通过文件记录。
# --------------------------------------------------------------------------
class _NullWriter(object):
    """黑洞写入器：静默丢弃所有写入。"""
    def write(self, data):
        pass
    def flush(self):
        pass

try:
    sys.stdout = _NullWriter()
    sys.stderr = _NullWriter()
except Exception:
    pass

logger.info("=== SpaceClaim Transit Script 启动 ===")
logger.info("日志文件: {}".format(logger.log_file))
logger.info("Python: {}".format(sys.version))
logger.info("sys.argv: {}".format(sys.argv))

# --------------------------------------------------------------------------
# 1. 导入 SpaceClaim API V23
# --------------------------------------------------------------------------
# 注意：Body1/Body2 等是 .scscript 格式独有的会话全局变量，
#       无法通过 from SpaceClaim.Api.V23 import Body1 导入。
#       在 .py 脚本中应使用 None（表示"所有体"），
#       对于单体的 STEP 导入模型与 Body1 语义完全等价。
try:
    import SpaceClaim.Api.V23 as _sc_api
    from SpaceClaim.Api.V23 import *
    logger.info("SpaceClaim.Api.V23 导入成功")
except ImportError as e:
    logger.critical("无法导入 SpaceClaim.Api.V23: {}".format(e))
    logger.critical("请确认 SpaceClaim 2023 R1 已正确安装，且脚本在 SpaceClaim 内部运行")
    sys.exit(1)


# ============================================================================
# 脚本参数获取
# ============================================================================
# SpaceClaim 支持多种脚本调用方式，参数传递格式各不相同：
#
# 方式A: /RunScript + /ScriptArgs（命令行模式）
#   args 被注入为列表 ["config", "stepdir", "scdocdir"]
#
# 方式B: Application.RunScript(scriptPath, argDictionary)（API 模式，C# 桥接）
#   args 被注入为字典 {"config_name": "6", "step_dir": "...", "scdoc_dir": "..."}
#
# 方式C: sys.argv（交互式测试）
#   sys.argv = ["script.py", "6", step_dir, scdoc_dir]
#
# 方式D: 环境变量（后备方案）
#   AUTOFLUID_SC_CONFIG, AUTOFLUID_SC_STEP_DIR, AUTOFLUID_SC_SCDOC_DIR
def _get_script_args():
    """获取脚本参数列表。兼容多种调用模式。"""
    # 方式1: SpaceClaim 注入的全局 args
    g = globals()
    if 'args' in g:
        raw_args = g['args']

        # 方式1a: Dictionary 形式（Application.RunScript API 模式）
        #   argDictionary = {"config_name": ..., "step_dir": ..., "scdoc_dir": ...}
        if isinstance(raw_args, dict) or hasattr(raw_args, 'get'):
            # ★ 不可使用 "or" 短路求值：config_name 可能是整数 0（falsy），
            #    会被 "or" 错误跳过。应显式检查 None。
            config = raw_args.get('config_name')
            if config is None:
                config = raw_args.get('configName')
            if config is None:
                config = raw_args.get('config')
            step_dir = raw_args.get('step_dir') or raw_args.get('stepDir')
            scdoc_dir = raw_args.get('scdoc_dir') or raw_args.get('scdocDir')
            if config is not None and step_dir and scdoc_dir:
                logger.info("参数来源: Application.RunScript Dictionary")
                return [str(config), str(step_dir), str(scdoc_dir)]
            logger.warning("Dictionary args 缺少必要字段: keys={}".format(
                list(raw_args.keys()) if hasattr(raw_args, 'keys') else 'N/A'))

        # 方式1b: List/Tuple 形式（/RunScript + /ScriptArgs 命令行模式）
        elif isinstance(raw_args, (list, tuple)):
            logger.info("参数来源: /RunScript List")
            return list(raw_args)

    # 方式2: 检查内置作用域（IronPython 兼容）
    try:
        import __builtin__
        if hasattr(__builtin__, 'args'):
            return list(__builtin__.args)
    except ImportError:
        pass

    # 方式3: 通过 sys.argv（去掉脚本路径本身）
    if len(sys.argv) > 1:
        return sys.argv[1:]

    # 方式4: 通过环境变量传递（最后的后备方案）
    env_config = os.environ.get("AUTOFLUID_SC_CONFIG", "")
    env_step = os.environ.get("AUTOFLUID_SC_STEP_DIR", "")
    env_scdoc = os.environ.get("AUTOFLUID_SC_SCDOC_DIR", "")
    if env_config and env_step and env_scdoc:
        return [env_config, env_step, env_scdoc]

    return []


# ============================================================================
# 文档关闭辅助
# ============================================================================

# 常驻模式下已处理的构型计数（跨 process_step_file 调用持久化，仅用于日志）
_persistent_config_count = 0


def _get_document_count():
    """获取 SpaceClaim 中当前打开的文档数量（通过所有窗口计数）。

    SpaceClaim API V23 没有 Document.All 属性。正确的做法是通过
    Window.AllWindows（ICollection<Window>）按窗口计数，
    每个窗口关联一个 Document。

    Returns:
        int: 打开的窗口数（≈文档数）；若 API 不可用返回 -1 表示未知。
    """
    try:
        all_wins = list(Window.AllWindows)
        return len(all_wins)
    except Exception:
        return -1


def _force_gc():
    """强制执行 .NET 垃圾回收，释放 IronPython 持有的 .NET 对象引用。

    在 IronPython/SpaceClaim 环境中，Python 变量（如 doc、part、
    bodies、Selection）即使超出作用域，.NET GC 也可能不会立即
    回收这些对象。显式调用 GC 有助于释放这些引用占用的内存。

    注意：SpaceClaim 内嵌的 IronPython 中 clr.System 可能不可用，
    需通过 clr.AddReference + import System 访问 .NET GC。
    """
    try:
        import clr
        clr.AddReference("System")
        import System
        System.GC.Collect()
        System.GC.WaitForPendingFinalizers()
        System.GC.Collect()  # 二次收集：回收已终结的对象
        logger.debug(".NET GC 已强制执行")
    except Exception as e:
        logger.debug("强制 GC 失败（非关键）: {}".format(e))


def _close_all_documents():
    """关闭 SpaceClaim 中所有打开的窗口/文档，并验证关闭结果。

    SpaceClaim API V23 的正确做法：
    1. 通过 Window.AllWindows 获取所有窗口（ICollection<Window>）
    2. 逐窗口调用 Window.Close() 关闭
    3. 反复执行直到窗口数为 0
    4. 辅以 CloseWindow / CloseAll 命令兜底
    5. 强制执行 .NET GC 释放残留引用
    6. 验证最终窗口数

    Document 对象没有 Close() 方法，也没有 Document.All 属性；
    正确关闭文档的唯一方式是通过其关联的 Window.Close()。

    Returns:
        int: 关闭后残留的窗口数（0 = 完全清理成功）。
    """
    doc_count_before = _get_document_count()
    if doc_count_before <= 0:
        # 无法统计或确实无窗口 → 仍需执行兜底清理
        logger.debug("关闭前窗口数: {}（未知或无窗口），执行兜底清理...".format(
            doc_count_before))
        # 兜底：如果计数失败（API 可能异常），仍尝试命令方式清理
        try:
            Command.Execute("CloseAll")
            time.sleep(0.5)
        except Exception:
            pass
        _force_gc()
        # 重新计数
        after = _get_document_count()
        logger.debug("兜底清理后窗口数: {}".format(after))
        return after if after >= 0 else 0

    logger.info("关闭前打开的窗口数: {}".format(doc_count_before))

    # ---- 第1层: 通过 Window.AllWindows 逐窗口关闭（★ 核心方法）----
    close_count = 0
    try:
        all_windows = list(Window.AllWindows)
        logger.info("枚举到 {} 个窗口，逐窗口关闭...".format(len(all_windows)))
        for win in all_windows:
            try:
                # Window.Close() — 这是 V23 API 中关闭窗口/文档的正确方法
                win.Close()
                close_count += 1
                logger.debug("Window.Close() 调用成功 ({}/{})".format(
                    close_count, len(all_windows)))
                # 短暂等待 GUI 处理关闭事件
                time.sleep(0.15)
            except Exception as ex:
                logger.debug("Window.Close() 异常: {}: {}".format(
                    type(ex).__name__, ex))
        if close_count > 0:
            logger.info("第1层: 已关闭 {} 个窗口".format(close_count))
            time.sleep(0.5)
    except Exception as e:
        logger.warning("枚举 Window.AllWindows 关闭时异常: {}: {}".format(
            type(e).__name__, e))

    # ---- 第2层: 反复执行 CloseWindow 命令，确保全部关闭 ----
    max_close_attempts = max(doc_count_before, 5)
    for attempt in range(max_close_attempts):
        remaining = _get_document_count()
        if remaining <= 0:
            logger.info("第2层: 第{}次检查时窗口数已为 0".format(attempt + 1))
            break
        try:
            Command.Execute("CloseWindow")
            time.sleep(0.3)
            remaining_after = _get_document_count()
            logger.debug("第2层: 第{}次 CloseWindow 后窗口数: {}".format(
                attempt + 1, remaining_after))
            if remaining_after <= 0:
                break
        except Exception as e:
            logger.debug("第2层: CloseWindow 异常（可能无窗口可关）: {}".format(e))
            break

    # ---- 第3层: CloseAll 命令兜底 ----
    try:
        Command.Execute("CloseAll")
        time.sleep(0.5)
        remaining = _get_document_count()
        logger.debug("第3层: CloseAll 后窗口数: {}".format(remaining))
    except Exception:
        pass

    # ---- 第4层: 强制 .NET GC ----
    _force_gc()

    # ---- 验证 ----
    doc_count_after = _get_document_count()
    if doc_count_after > 0:
        logger.warning(
            "⚠ 仍有 {} 个窗口未关闭！内存可能持续累积".format(
                doc_count_after))
    else:
        logger.info("所有窗口已关闭（窗口数: 0）")

    return doc_count_after


# ============================================================================
# 主处理逻辑
# ============================================================================

def process_step_file(config_name, step_dir, scdoc_dir):
    """
    处理单个 STEP 文件：打开 → 创建命名选择集 → 保存 SCDOC。

    Args:
        config_name: 构型编号（整数或字符串）
        step_dir: STEP 文件目录
        scdoc_dir: SCDOC 输出目录
    """
    file_index = int(config_name)

    # 构造输入文件名 (格式: model_gen4.SLDPRT_XX.step)
    step_filename = "model_gen4.SLDPRT_{}.step".format(file_index)
    step_path = os.path.join(step_dir, step_filename)

    logger.info("正在处理构型 {}".format(file_index))
    logger.info("输入文件: {}".format(step_path))

    # ------------------------------------------------------------------
    # 1. 检查输入文件
    # ------------------------------------------------------------------
    if not os.path.exists(step_path):
        logger.error("输入文件不存在: {}".format(step_path))
        return False

    file_size = os.path.getsize(step_path)
    logger.info("文件大小: {} bytes".format(file_size))

    # ------------------------------------------------------------------
    # 2. 打开文档
    # ------------------------------------------------------------------
    logger.info("正在打开文档...")
    try:
        doc = Document.Open(step_path, None)
        if doc is None:
            logger.error("Document.Open 返回 None")
            return False
        logger.info("文档已打开")
    except Exception as e:
        logger.error("打开文档失败: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
        return False

    # ------------------------------------------------------------------
    # 3. 获取主部件 + 构造体选择集（替代 .scscript 的 Body1 隐式转换）
    # ------------------------------------------------------------------
    try:
        part = doc.MainPart
        if part is None:
            logger.error("无法获取文档主部件 (MainPart 为 None)")
            return False
        logger.info("已获取主部件")
    except Exception as e:
        logger.error("获取 MainPart 失败: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
        return False

    # PowerSelectOptions 第二参数类型为 ISelection，非 IBody。
    # .scscript 中 Body1 被 SpaceClaim 引擎隐式转换为 ISelection；
    # .py 中必须手动通过 Selection.Create(body) 构造。
    body_selection = None
    try:
        bodies = list(part.Bodies)
        if bodies:
            body_selection = Selection.Create(bodies[0])
            logger.info("已构造体选择集 (Bodies[0])")
    except Exception as e:
        logger.warning("构造体选择集失败: {}: {}".format(type(e).__name__, e))

    if body_selection is None:
        logger.warning("无体选择集，PowerSelectOptions 将不传第二参数（全选）")

    # ------------------------------------------------------------------
    # 4. 几何处理 — 创建命名选择集
    # ------------------------------------------------------------------
    # 说明：通过面面积范围自动选择面，创建命名选择集。
    # 面积单位为 mm²，MM2() 将数值转换为 API 内部单位。
    # 选择集自动命名：组1~组N；合并后重命名为英文名。
    logger.info("正在创建命名选择集...")

    def _create_named_selection(min_area_mm2, max_area_mm2):
        """创建一个基于面面积的命名选择集。

        .py 与 .scscript 的关键差异：
        PowerSelectOptions(append, selection) 第二参数是 ISelection。
        .scscript 中 Body1 (IBody) 被引擎隐式转换为 ISelection；
        .py 中必须用 Selection.Create(body) 显式构造 ISelection。
        """
        try:
            if body_selection is not None:
                opts = PowerSelectOptions(False, body_selection)
            else:
                opts = PowerSelectOptions(False)

            result = NamedSelection.Create(
                PowerSelection.Faces.ByAreaRange(
                    MM2(min_area_mm2), MM2(max_area_mm2),
                    opts,
                ),
                Selection.Empty(),
            )
            return result
        except Exception as e:
            logger.warning("创建选择集失败 (面积 {}-{}): {}: {}".format(min_area_mm2, max_area_mm2, type(e).__name__, e))
            return None

    # 按原始脚本顺序创建选择集
    selection_specs = [
        (8.55, 8.56),      # → 组1
        (6.71, 6.72),      # → 组2
        (9.54, 9.55),      # → 组3
        (17222, 17223),    # → 组4
        (28520, 28521),    # → 组5
    ]

    for i, (lo, hi) in enumerate(selection_specs, 1):
        result = _create_named_selection(lo, hi)
        if result is not None:
            logger.info("组{} 创建成功 (面积 {}-{} mm²)".format(i, lo, hi))
        else:
            logger.error("组{} 创建失败 (面积 {}-{} mm²)".format(i, lo, hi))

    # ------------------------------------------------------------------
    # 4b. 合并组4和组5（wall_chamber 和 wall_throat 的过渡段合并）
    # ------------------------------------------------------------------
    logger.info("正在合并 组4 和 组5...")
    try:
        NamedSelection.Merge("组4", "组5")
        logger.info("组4+组5 合并成功")
    except Exception as e:
        logger.warning("合并 组4+组5 失败: {}: {}".format(type(e).__name__, e))
        logger.warning("将跳过合并，这可能导致后续重命名映射偏移")

    # ------------------------------------------------------------------
    # 4c. 继续创建剩余选择集
    # ------------------------------------------------------------------
    remaining_specs = [
        (2116, 2117),      # Merge 后自动命名为 组5
        (33927, 33928),    # → 组6
        (25409, 25410),    # → 组7
        (12196, 12197),    # → 组8
    ]

    for lo, hi in remaining_specs:
        result = _create_named_selection(lo, hi)
        if result is not None:
            logger.info("选择集创建成功 (面积 {}-{} mm²)".format(lo, hi))
        else:
            logger.error("选择集创建失败 (面积 {}-{} mm²)".format(lo, hi))

    # ------------------------------------------------------------------
    # 5. 重命名选择集为英文名
    # ------------------------------------------------------------------
    # 说明：Merge 后 组5 被移除，下一个创建的选择集自动填补为 组5。
    #       因此重命名映射维持原始顺序即可。
    logger.info("正在重命名选择集...")
    rename_map = {
        "组1": "inlet_oxidizer",
        "组2": "inlet_fuel",
        "组3": "wall_gap",
        "组4": "wall_chamber",
        "组5": "wall_throat",
        "组6": "wall_nozzle",
        "组7": "outlet",
        "组8": "wall_top",
    }

    rename_success = 0
    rename_fail = 0
    for old_name, new_name in rename_map.items():
        try:
            result = NamedSelection.Rename(old_name, new_name)
            logger.info("  {} → {}".format(old_name, new_name))
            rename_success += 1
        except Exception as e:
            logger.warning("重命名 {} → {} 失败: {}: {}".format(old_name, new_name, type(e).__name__, e))
            rename_fail += 1

    if rename_fail > 0:
        logger.warning("{} 个选择集重命名失败，将以默认名称保存".format(rename_fail))

    # ------------------------------------------------------------------
    # 6. 保存文档为 SCDOC
    # ------------------------------------------------------------------
    out_filename = "model_gen4_{}.scdoc".format(file_index)
    out_path = os.path.join(scdoc_dir, out_filename)

    # 确保输出目录存在（Python 2.7 无 exist_ok 参数）
    try:
        if not os.path.isdir(scdoc_dir):
            os.makedirs(scdoc_dir)
    except (OSError, IOError):
        pass

    logger.info("正在保存文档: {}".format(out_path))
    try:
        doc.SaveAs(out_path)
        logger.info("文档已保存")
    except Exception as e:
        logger.error("保存文档失败: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
        return False

    # ------------------------------------------------------------------
    # 7. 验证输出文件
    # ------------------------------------------------------------------
    if os.path.exists(out_path):
        out_size = os.path.getsize(out_path)
        logger.info("输出文件验证通过: {} ({} bytes)".format(out_path, out_size))
    else:
        logger.error("输出文件未生成: {}".format(out_path))
        return False

    # ------------------------------------------------------------------
    # 8. 关闭文档释放资源
    # ------------------------------------------------------------------
    # 不关闭文档会导致 SpaceClaim 内部文档句柄/内存累积，
    # 约 2-3 个文档后进程因资源耗尽崩溃。
    # 使用 _close_all_documents() 确保所有文档（不仅是当前处理的）
    # 都被关闭，防止残留文档累积。
    try:
        if doc is not None:
            # ★ 先关闭所有文档（防止前次迭代的残留文档未释放）
            remaining = _close_all_documents()
            logger.info("文档关闭完成，残留文档数: {}".format(remaining))

            # ★ 释放本地引用，帮助 IronPython GC 回收 .NET 对象
            #    将 doc/part/body_selection 等引用置 None，
            #    避免 IronPython 持有引用阻止 .NET GC
            doc = None
            part = None
            body_selection = None
            _force_gc()
    except Exception as e:
        logger.warning("关闭文档失败（不影响结果）: {}: {}".format(type(e).__name__, e))

    logger.info("构型 {} 处理完成: {}".format(file_index, out_filename))
    return True


# ===========================================================================
# 脚本入口
# ============================================================================
# 说明：SpaceClaim 在 /RunScript 模式下会执行整个脚本文件。
#       脚本末尾的 Main() 调用确保在作为 .py 文件直接运行时也能正常工作。
#       SpaceClaim 不会自动调用 Main()，因此不存在双重执行问题。

# ---- 常驻模式：文件协议 IPC ----
# Bridge 启动 SpaceClaim 时设置 AUTOFLUID_SC_PERSISTENT=1，
# 脚本进入命令轮询循环，SpaceClaim 进程不退出。
#
# 文件协议：
#   命令: sc_cmd_{slot_id}.json              -> {"command":"process","run_id":"...","config":3,...}
#   结果: sc_result_{slot_id}_{run_id}.json  -> {"config":"3","run_id":"...","success":true,...}
#   就绪: sc_ready_{slot_id}.json            -> {"ready":true,"slot_id":0}
#   退出: sc_cmd_{slot_id}.json              -> {"command":"quit"}
_persistent_initialized = False

def _persistent_loop():
    """常驻模式主循环：轮询命令文件，执行转换，写入 per-run 结果文件。"""
    global _persistent_initialized
    if _persistent_initialized:
        return
    _persistent_initialized = True

    cmd_dir = os.environ.get("AUTOFLUID_SC_CMD_DIR", "")
    slot_id = os.environ.get("AUTOFLUID_SC_SLOT_ID", "0")

    if not cmd_dir:
        logger.error("常驻模式: AUTOFLUID_SC_CMD_DIR 未设置")
        return

    cmd_file = os.path.join(cmd_dir, "sc_cmd_{}.json".format(slot_id))
    ready_file = os.path.join(cmd_dir, "sc_ready_{}.json".format(slot_id))

    # 写入就绪标志
    try:
        with open(ready_file, "w") as f:
            f.write('{"ready":true,"slot_id":' + str(slot_id) + '}\n')
        logger.info("常驻模式: 就绪标志已写入 {}".format(ready_file))
    except (IOError, OSError) as e:
        logger.error("常驻模式: 写入就绪标志失败: {}".format(e))
        return

    poll_interval = 1.0
    logger.info("常驻模式: 开始轮询命令文件 {}".format(cmd_file))

    # ★ 基于 slot_id 的默认路径，确保外层 handler 在任何异常下都能写入有效路径
    result_file = os.path.join(cmd_dir, "sc_result_{}".format(slot_id))

    # ★ 使用全局计数器追踪已处理的构型数（跨命令循环迭代持久化）
    global _persistent_config_count
    _persistent_config_count = 0

    while True:
        try:
            if not os.path.exists(cmd_file):
                time.sleep(poll_interval)
                continue

            # 读取命令
            try:
                with open(cmd_file, "r") as f:
                    cmd_data = json.load(f)
            except (ValueError, IOError, OSError) as e:
                logger.error("常驻模式: 读取命令文件失败: {}".format(e))
                try:
                    os.remove(cmd_file)
                except (IOError, OSError):
                    pass
                continue

            # 检查退出命令
            if cmd_data.get("command") == "quit":
                logger.info("常驻模式: 收到退出命令")
                try:
                    os.remove(cmd_file)
                except (IOError, OSError):
                    pass
                break

            config_name = str(cmd_data.get("config", ""))
            step_dir = cmd_data.get("stepdir", "")
            scdoc_dir = cmd_data.get("scdocdir", "")
            run_id = cmd_data.get("run_id", "")

            # ★ per-run 结果文件：每次命令唯一 run_id，消除跨构型竞态
            if run_id:
                result_file = os.path.join(
                    cmd_dir, "sc_result_{}_{}".format(slot_id, run_id))
            else:
                # 兼容无 run_id 的旧命令格式
                result_file = os.path.join(
                    cmd_dir, "sc_result_{}".format(slot_id))

            if not config_name or not step_dir or not scdoc_dir:
                logger.error("常驻模式: 命令缺少必要字段: {}".format(cmd_data))
                _write_result(result_file, config_name, False,
                              "命令缺少必要字段", run_id=run_id)
                try:
                    os.remove(cmd_file)
                except (IOError, OSError):
                    pass
                continue

            # 清理命令文件（已读取）
            try:
                os.remove(cmd_file)
            except (IOError, OSError):
                pass

            # ---- ★ 构型前清理：确保 SpaceClaim 处于干净状态 ----
            # 在打开新 STEP 前关闭所有残留文档，防止内存累积。
            # 即使上一个构型的关闭逻辑因异常跳过，本步骤也能兜底清理。
            _persistent_config_count += 1
            logger.info("=" * 40)
            logger.info("常驻模式: 准备处理第 {} 个构型 {} (run={})".format(
                _persistent_config_count, config_name, run_id))
            logger.info("STEP 目录: {}".format(step_dir))
            logger.info("SCDOC 目录: {}".format(scdoc_dir))

            # ★ 构型前强制清理所有文档 + GC
            try:
                doc_count_before = _get_document_count()
                if doc_count_before > 0:
                    logger.warning(
                        "⚠ 处理前发现 {} 个残留文档，先行清理...".format(
                            doc_count_before))
                    _close_all_documents()
                _force_gc()
            except Exception as e:
                logger.warning("构型前清理异常（非关键）: {}: {}".format(
                    type(e).__name__, e))

            success = process_step_file(config_name, step_dir, scdoc_dir)

            # ★ _write_result 独立包裹：编码异常不逃逸到外层 BaseException handler
            #   _write_result 内部已有 4 层降级保护，此处仅兜底极端编码异常
            try:
                _write_result(result_file, config_name, success,
                              "转换成功" if success else "转换失败",
                              run_id=run_id)
            except (UnicodeEncodeError, UnicodeDecodeError) as write_err:
                # 编码异常：_write_result 的降级路径可能已写入部分结果，
                # 但不应让外层 handler 再写入 success=false 覆盖
                try:
                    logger.error("常驻模式: 结果写入编码异常（已忽略）: {}".format(write_err))
                except Exception:
                    pass

            # ★ 日志调用单独包裹：即使日志器因编码问题崩溃，
            #   也不会导致异常传播到外层 except（那会覆盖已写入的结果文件）
            try:
                if success:
                    logger.info("常驻模式: 构型 {} 转换成功".format(config_name))
                else:
                    logger.error("常驻模式: 构型 {} 转换失败".format(config_name))
            except Exception:
                pass

        except BaseException as e:
            # ★ 使用 BaseException 而非 Exception：
            #   在 IronPython/SpaceClaim 环境中，traceback.print_exc() 可能抛出
            #   .NET 层面的 SystemException 等非 Python Exception 子类。
            #   若仅捕获 Exception，这些异常会绕过处理器直接终止脚本。
            try:
                logger.critical("常驻模式: 主循环异常: {}: {}".format(
                    type(e).__name__, e))
            except Exception:
                pass
            try:
                traceback.print_exc()
            except Exception:
                pass
            # 写入错误结果（使用最近的 result_file）
            try:
                _write_result(result_file, "", False,
                              "主循环异常: {}: {}".format(type(e).__name__, e))
            except Exception:
                pass

    logger.info("常驻模式: 退出命令循环（已处理 {} 个构型）".format(
        _persistent_config_count))


def _write_result(result_file, config_name, success, message="",
                  run_id=None):
    """写入结果文件。

    使用原子写入模式：先写 .tmp 文件再 rename，确保 Python 端
    不会读到半写状态的文件。

    ★ IronPython 2.7 兼容性：
      - io.open() 的 errors="replace" 在 IronPython 中可能不生效，
        改用 codecs.open()（已在 logger 中验证可靠）。
      - os.fsync(f.fileno()) 在 IronPython 中 .fileno() 可能返回
        .NET 对象而非 int，单独包裹避免影响主写入流程。
      - 使用 json.dumps + ensure_ascii=True（Python 2 默认行为）
        预编码为 ASCII 安全形式，彻底避免 UnicodeEncodeError。
    """
    result = {
        "config": config_name,
        "success": bool(success),
        "message": str(message),
        "timestamp": time.time(),
    }
    if run_id:
        result["run_id"] = run_id

    # ★ 预编码为 JSON 字符串（Python 2 的 json.dumps 默认 ensure_ascii=True，
    #    所有非 ASCII 字符会被转义为 \uXXXX，彻底避免编码问题）
    try:
        json_text = json.dumps(result) + "\n"
    except (TypeError, ValueError, UnicodeDecodeError) as e:
        # 降级：仅写入 ASCII 安全字段
        logger.error("JSON 序列化失败: {}: {}".format(type(e).__name__, e))
        try:
            fallback = {
                "config": str(config_name),
                "success": bool(success),
                "message": "json_serialize_error",
                "timestamp": time.time(),
            }
            if run_id:
                fallback["run_id"] = run_id
            json_text = json.dumps(fallback) + "\n"
        except Exception:
            # 最终降级：手动构造最小 JSON
            json_text = (
                '{"config":' + json.dumps(str(config_name)) +
                ',"success":' + ("true" if success else "false") +
                ',"message":"json_fatal_error",' +
                '"timestamp":' + str(time.time()) + '}\n'
            )

    try:
        tmp_file = result_file + ".tmp"
        # ★ 使用 codecs.open 替代 io.open：
        #    IronPython 2.7 的 io.open 不一定将 errors 参数传递到底层
        #    StreamWriter；codecs.open 对 errors 参数的支持更可靠。
        with codecs.open(tmp_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(json_text)
            f.flush()
            # ★ fsync 单独包裹：.fileno() 在 IronPython 中可能返回
            #    .NET 对象而非 int，导致 os.fsync 失败。
            #    不影响写入——flush 已确保数据从缓冲区写出。
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

        # Windows 下 rename 目标存在时会报错，先删除
        try:
            if os.path.exists(result_file):
                os.remove(result_file)
        except (IOError, OSError):
            pass
        os.rename(tmp_file, result_file)
        logger.debug("结果文件已写入: {}".format(result_file))

    except Exception as e:
        # 捕获所有异常（包括 UnicodeEncodeError），确保不会因编码问题
        # 导致结果文件完全丢失。尝试以纯 ASCII 方式降级写入。
        logger.error("写入结果文件失败: {}: {}".format(type(e).__name__, e))
        try:
            fallback_json = (
                '{"config":"' + str(config_name).replace('"', '\\"') +
                '","success":' + ("true" if success else "false") +
                ',"message":"result_write_error",' +
                '"timestamp":' + str(time.time()) + '}\n'
            )
            tmp_file = result_file + ".tmp"
            # ★ 直接用内置 open 写二进制：Python 2 的 str 即 bytes，
            #    写入纯 ASCII 内容不会触发任何编码转换。
            with open(tmp_file, "wb") as f:
                f.write(fallback_json.encode("ascii", errors="replace"))
                f.flush()
            try:
                if os.path.exists(result_file):
                    os.remove(result_file)
            except (IOError, OSError):
                pass
            os.rename(tmp_file, result_file)
            logger.info("降级写入结果文件成功: {}".format(result_file))
        except Exception as e2:
            logger.error("降级写入也失败: {}: {}".format(type(e2).__name__, e2))


def Main():
    """脚本主入口。由脚本末尾显式调用。"""
    try:
        # 获取脚本参数
        script_args = _get_script_args()

        if not script_args or len(script_args) < 3:
            logger.error("=" * 60)
            logger.error("参数不足！")
            logger.info("用法: SpaceClaim.exe /RunScript=<脚本> /ScriptArgs=<构型名> <STEP目录> <SCDOC输出目录>")
            logger.info("或设置环境变量: AUTOFLUID_SC_CONFIG / AUTOFLUID_SC_STEP_DIR / AUTOFLUID_SC_SCDOC_DIR")
            logger.error("实际收到的参数 ({} 个): {}".format(len(script_args), script_args))
            logger.error("=" * 60)
            return

        config_name = script_args[0]
        step_dir = script_args[1]
        scdoc_dir = script_args[2]

        logger.info("=" * 60)
        logger.info("SpaceClaim Transit Script V23")
        logger.info("构型编号: {}".format(config_name))
        logger.info("STEP 目录: {}".format(step_dir))
        logger.info("SCDOC 目录: {}".format(scdoc_dir))
        logger.info("=" * 60)

        success = process_step_file(config_name, step_dir, scdoc_dir)

        if success:
            logger.info("构型 {} 转换成功".format(config_name))
        else:
            logger.error("构型 {} 转换失败".format(config_name))

    except Exception as e:
        logger.critical("脚本执行异常: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()

    finally:
        # ★ 脚本执行完毕后退出 SpaceClaim（/RunScript 模式会自动退出，
        #    但显式调用确保在任何情况下 SpaceClaim 都能正常关闭，
        #    避免残留进程影响下次启动）
        # 若设置了环境变量 AUTOFLUID_SC_NOEXIT=1，则跳过退出（用于交互式调试）
        if os.environ.get("AUTOFLUID_SC_NOEXIT", "") != "1":
            try:
                logger.info("正在退出 SpaceClaim...")
                Command.Execute("Exit")
            except Exception as e_exit:
                logger.warning("退出 SpaceClaim 时异常（可能已在关闭中）: {}: {}".format(
                    type(e_exit).__name__, e_exit))
        else:
            logger.info("AUTOFLUID_SC_NOEXIT=1，跳过退出 SpaceClaim")


# ---- 脚本入口分发 ----
# 常驻模式（AUTOFLUID_SC_PERSISTENT=1）：进入文件协议 IPC 循环
# 一次性模式（默认）：执行 Main()
if os.environ.get("AUTOFLUID_SC_PERSISTENT", "") == "1":
    _persistent_loop()
else:
    Main()
