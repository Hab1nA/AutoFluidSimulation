# -*- coding: utf-8 -*-
# ============================================================================
# SpaceClaim Transit Script — V23 兼容版本
# 功能：读取 STEP 文件 → 创建命名选择集 → 保存为 SCDOC
#
# 用法：SpaceClaim.exe /RunScript="<本脚本路径>" /ScriptArgs="<构型名> <STEP目录> <SCDOC输出目录>"
#   args[0] = config_name   (构型编号，整数)
#   args[1] = step_dir      (STEP 文件所在目录)
#   args[2] = scdoc_dir     (SCDOC 输出目录)
#
# 兼容版本：SpaceClaim 2023 R1 (API V23)
# 说明：args 是 SpaceClaim 在 /RunScript 模式下自动注入的全局变量，
#       包含 /ScriptArgs 中以空格分隔的参数列表。
#
# 调试：诊断日志写入 %TEMP%\spaceclaim_transit_debug.log
#       SpaceClaim 是 GUI 程序，print() 输出可能不会显示在终端，
#       请查看该日志文件获取脚本执行详情。
# ============================================================================
import os
import sys
import io
import traceback
from datetime import datetime

# --------------------------------------------------------------------------
# 0. 尽早建立日志文件（在任何可能失败的导入之前）
# --------------------------------------------------------------------------
_LOG_FILE = os.path.join(
    os.environ.get("TEMP", os.path.dirname(os.path.abspath(__file__))),
    "spaceclaim_transit_debug.log",
)

def _log(msg):
    """同时写入日志文件和 print（print 在 SpaceClaim GUI 下可能不可见）。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(timestamp, msg)
    try:
        with io.open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (IOError, OSError):
        pass
    print(line)

_log("=== SpaceClaim Transit Script 启动 ===")
_log("日志文件: {}".format(_LOG_FILE))
_log("Python: {}".format(sys.version))
_log("sys.argv: {}".format(sys.argv))

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
    _log("[OK] SpaceClaim.Api.V23 导入成功")
except ImportError as e:
    _log("[FATAL] 无法导入 SpaceClaim.Api.V23: {}".format(e))
    _log("请确认 SpaceClaim 2023 R1 已正确安装，且脚本在 SpaceClaim 内部运行")
    sys.exit(1)


# ============================================================================
# 脚本参数获取
# ============================================================================
# SpaceClaim 在 /RunScript 模式下会将 /ScriptArgs 的值按空格分割，
# 存入全局变量 args。若交互式测试或其他模式运行，则回退到 sys.argv。
def _get_script_args():
    """获取脚本参数列表。优先使用 SpaceClaim 注入的全局 args。"""
    # 方式1: SpaceClaim 注入的全局 args（/RunScript + /ScriptArgs）
    g = globals()
    if 'args' in g and isinstance(g['args'], (list, tuple)):
        return list(g['args'])

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

    return []


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

    _log("[INFO] 正在处理构型 {}".format(file_index))
    _log("[INFO]   输入文件: {}".format(step_path))

    # ------------------------------------------------------------------
    # 1. 检查输入文件
    # ------------------------------------------------------------------
    if not os.path.exists(step_path):
        _log("[ERROR] 输入文件不存在: {}".format(step_path))
        return False

    file_size = os.path.getsize(step_path)
    _log("[INFO]   文件大小: {} bytes".format(file_size))

    # ------------------------------------------------------------------
    # 2. 打开文档
    # ------------------------------------------------------------------
    _log("[INFO] 正在打开文档...")
    try:
        doc = Document.Open(step_path, None)
        if doc is None:
            _log("[ERROR] Document.Open 返回 None")
            return False
        _log("[INFO] ✓ 文档已打开")
    except Exception as e:
        _log("[ERROR] 打开文档失败: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
        return False

    # ------------------------------------------------------------------
    # 3. 获取主部件 + 构造体选择集（替代 .scscript 的 Body1 隐式转换）
    # ------------------------------------------------------------------
    try:
        part = doc.MainPart
        if part is None:
            _log("[ERROR] 无法获取文档主部件 (MainPart 为 None)")
            return False
        _log("[INFO] 已获取主部件")
    except Exception as e:
        _log("[ERROR] 获取 MainPart 失败: {}: {}".format(type(e).__name__, e))
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
            _log("[INFO] 已构造体选择集 (Bodies[0])")
    except Exception as e:
        _log("[WARN] 构造体选择集失败: {}: {}".format(type(e).__name__, e))

    if body_selection is None:
        _log("[WARN] 无体选择集，PowerSelectOptions 将不传第二参数（全选）")

    # ------------------------------------------------------------------
    # 4. 几何处理 — 创建命名选择集
    # ------------------------------------------------------------------
    # 说明：通过面面积范围自动选择面，创建命名选择集。
    # 面积单位为 mm²，MM2() 将数值转换为 API 内部单位。
    # 选择集自动命名：组1~组N；合并后重命名为英文名。
    _log("[INFO] 正在创建命名选择集...")

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
            _log("[WARN] 创建选择集失败 (面积 {}-{}): {}: {}".format(min_area_mm2, max_area_mm2, type(e).__name__, e))
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
            _log("[INFO]   ✓ 组{} 创建成功 (面积 {}-{} mm²)".format(i, lo, hi))
        else:
            _log("[ERROR]   组{} 创建失败 (面积 {}-{} mm²)".format(i, lo, hi))

    # ------------------------------------------------------------------
    # 4b. 合并组4和组5（wall_chamber 和 wall_throat 的过渡段合并）
    # ------------------------------------------------------------------
    _log("[INFO] 正在合并 组4 和 组5...")
    try:
        merge_result = NamedSelection.Merge("组4", "组5")
        _log("[INFO]   ✓ 组4+组5 合并成功")
    except Exception as e:
        _log("[WARN] 合并 组4+组5 失败: {}: {}".format(type(e).__name__, e))
        _log("[WARN] 将跳过合并，这可能导致后续重命名映射偏移")

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
            _log("[INFO]   选择集创建成功 (面积 {}-{} mm²)".format(lo, hi))
        else:
            _log("[ERROR]   选择集创建失败 (面积 {}-{} mm²)".format(lo, hi))

    # ------------------------------------------------------------------
    # 5. 重命名选择集为英文名
    # ------------------------------------------------------------------
    # 说明：Merge 后 组5 被移除，下一个创建的选择集自动填补为 组5。
    #       因此重命名映射维持原始顺序即可。
    _log("[INFO] 正在重命名选择集...")
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
            _log("[INFO]   {} → {}".format(old_name, new_name))
            rename_success += 1
        except Exception as e:
            _log("[WARN]   重命名 {} → {} 失败: {}: {}".format(old_name, new_name, type(e).__name__, e))
            rename_fail += 1

    if rename_fail > 0:
        _log("[WARN] {} 个选择集重命名失败，将以默认名称保存".format(rename_fail))

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

    _log("[INFO] 正在保存文档: {}".format(out_path))
    try:
        doc.SaveAs(out_path)
        _log("[INFO] 文档已保存")
    except Exception as e:
        _log("[ERROR] 保存文档失败: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()
        return False

    # ------------------------------------------------------------------
    # 7. 验证输出文件
    # ------------------------------------------------------------------
    if os.path.exists(out_path):
        out_size = os.path.getsize(out_path)
        _log("[INFO] 输出文件验证通过: {} ({} bytes)".format(out_path, out_size))
    else:
        _log("[ERROR] 输出文件未生成: {}".format(out_path))
        return False

    # ------------------------------------------------------------------
    # 8. 关闭文档
    # ------------------------------------------------------------------
    _log("[INFO] 正在关闭文档...")
    try:
        window = Window.ActiveWindow
        if window is not None:
            window.Close()
            _log("[INFO] 文档已关闭")
        else:
            _log("[INFO] 无活动窗口（可能已自动关闭）")
    except Exception as e:
        _log("[INFO] 关闭窗口时异常（可忽略）: {}: {}".format(type(e).__name__, e))

    _log("[SUCCESS] 构型 {} 处理完成: {}".format(file_index, out_filename))
    return True


# ============================================================================
# 脚本入口
# ============================================================================
# 说明：SpaceClaim 在 /RunScript 模式下会执行整个脚本文件。
#       脚本末尾的 Main() 调用确保在作为 .py 文件直接运行时也能正常工作。
#       SpaceClaim 不会自动调用 Main()，因此不存在双重执行问题。

def Main():
    """脚本主入口。由脚本末尾显式调用。"""
    try:
        # 获取脚本参数
        script_args = _get_script_args()

        if not script_args or len(script_args) < 3:
            _log("=" * 60)
            _log("[ERROR] 参数不足！")
            _log("用法: SpaceClaim.exe /RunScript=\"<脚本>\" /ScriptArgs=\"<构型名> <STEP目录> <SCDOC输出目录>\"")
            _log("实际收到的参数 ({} 个): {}".format(len(script_args), script_args))
            _log("=" * 60)
            return

        config_name = script_args[0]
        step_dir = script_args[1]
        scdoc_dir = script_args[2]

        _log("=" * 60)
        _log("SpaceClaim Transit Script V23")
        _log("  构型编号: {}".format(config_name))
        _log("  STEP 目录: {}".format(step_dir))
        _log("  SCDOC 目录: {}".format(scdoc_dir))
        _log("=" * 60)

        success = process_step_file(config_name, step_dir, scdoc_dir)

        if success:
            _log("\n[DONE] 构型 {} 转换成功".format(config_name))
        else:
            _log("\n[FAILED] 构型 {} 转换失败".format(config_name))

    except Exception as e:
        _log("\n[FATAL] 脚本执行异常: {}: {}".format(type(e).__name__, e))
        traceback.print_exc()


# 显式调用 Main() —— SpaceClaim 不会自动调用，此处确保脚本执行
Main()

