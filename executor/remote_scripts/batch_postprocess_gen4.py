"""
Fluent PostProcess 批处理脚本 - 参数化版本。

PostProcess 只负责在工作站本地生成后处理结果并写入完成 flag。
后续将结果上传到服务器的 Collect/Upload 阶段不在本脚本中执行。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import time
from typing import Any

os.environ["PYTHONUTF8"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import ansys.fluent.core as pyfluent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行 Fluent PostProcess 处理指定编号的模型。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("config_id", type=int, help="模型编号")
    parser.add_argument("--case-dir", type=str, required=True, help="Solver case/data 输出目录")
    parser.add_argument("--post-journal-path", type=str, required=True, help="后处理 Journal 文件")
    parser.add_argument(
        "--extra-post-journal-path",
        type=str,
        default=None,
        help="可选：额外后处理 Journal 文件",
    )
    parser.add_argument(
        "--postprocess-output-dir",
        type=str,
        required=True,
        help="后处理业务输出目录，供后续上传阶段扫描",
    )
    parser.add_argument("--flag-file", type=str, required=True, help="完成标志文件")
    parser.add_argument("--anim-dir", type=str, required=True, help="动画输出目录")
    parser.add_argument("--working-dir", type=str, required=True, help="Fluent 启动工作目录")
    parser.add_argument("--working-dir-t", type=str, required=True, help="温度动画工作目录")
    parser.add_argument("--working-dir-v", type=str, required=True, help="速度动画工作目录")
    parser.add_argument("--metrics-script", type=str, default=None, help="可选：五项指标 PyFluent 后处理脚本")
    parser.add_argument("--compute-metrics-script", type=str, default=None, help="可选：指标标量组合脚本")
    parser.add_argument("--metrics-processor-count", type=int, default=1, help="指标后处理 Fluent 核数")
    parser.add_argument("--metrics-ambient-pressure", type=float, default=0.0, help="推力压力项环境压力 Pa")
    parser.add_argument("--metrics-pressure-reference", type=float, default=101325.0, help="Fluent 表压转绝压参考 Pa")
    parser.add_argument("--metrics-tcomb", type=float, default=1000.0, help="混合比统计温度阈值 K")
    parser.add_argument("--metrics-thrust-axis", choices=("x", "y", "z"), default="x", help="推力轴向")
    parser.add_argument("--metrics-exit-to-throat-area-ratio", type=float, default=7.427276607, help="出口面积与喉部面积比 Ae/At")
    parser.add_argument("--metrics-cstar-reference", type=float, default=1830.4, help="CEA 或试验基准特征速度 m/s")
    return parser.parse_args()


def _require_file(path: str, label: str) -> None:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{label}不存在: {path}")


def _ensure_session_healthy(session: Any, stage: str) -> None:
    try:
        if session.is_server_healthy():
            print(f"[健康检查] Fluent server 正常: {stage}")
            return
    except Exception as e:
        raise RuntimeError(f"Fluent server 健康检查失败 ({stage}): {e}") from e
    raise RuntimeError(f"Fluent server 健康检查失败 ({stage}): server unhealthy")


def _read_case_data_file(session: Any, case_path: str) -> None:
    file_tui = session.tui.file
    read_case_data = getattr(file_tui, "read_case_data", None)
    if callable(read_case_data):
        read_case_data(case_path)
        return
    file_tui.read_case(case_path)


def _move_and_rename(config_id: int, working_dir_t: str, working_dir_v: str, anim_dir: str) -> None:
    files = [
        (os.path.join(working_dir_v, "animation-v.mp4"), f"v_gen4_{config_id}.mp4"),
        (os.path.join(working_dir_t, "animation-t.mp4"), f"t_gen4_{config_id}.mp4"),
    ]
    for src_path, dst_name in files:
        dst_path = os.path.join(anim_dir, dst_name)
        if not os.path.exists(src_path):
            print(f"[{config_id}] 未找到动画: {src_path}")
            continue
        if os.path.exists(dst_path):
            os.remove(dst_path)
        shutil.move(src_path, dst_path)
        print(f"[{config_id}] 移动动画: {src_path} -> {dst_path}")


def _cleanup_working_dirs(config_id: int, working_dirs: list[str]) -> None:
    for dir_path in working_dirs:
        if not os.path.exists(dir_path):
            print(f"[{config_id}] 目录不存在，无需清空: {dir_path}")
            continue
        for filename in os.listdir(dir_path):
            path = os.path.join(dir_path, filename)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
            except OSError as e:
                print(f"[{config_id}] 清理工作目录失败 {path}: {e}")


def _cleanup_log_files(config_id: int, log_dir: str) -> None:
    patterns = ["fluent-*.trn", "report-def-*-rfile*.out"]
    try:
        filenames = os.listdir(log_dir)
    except OSError as e:
        print(f"[{config_id}] 列出日志目录失败 {log_dir}: {e}")
        return
    for filename in filenames:
        if not any(fnmatch.fnmatchcase(filename.lower(), pattern) for pattern in patterns):
            continue
        path = os.path.join(log_dir, filename)
        try:
            os.remove(path)
            print(f"[{config_id}] 已删除日志: {filename}")
        except OSError as e:
            print(f"[{config_id}] 删除日志失败 {filename}: {e}")


def _close_session(config_id: int, session: Any) -> None:
    try:
        session.exit()
        return
    except Exception as cleanup_err:
        print(f"[{config_id}] Fluent 退出失败: {cleanup_err}")
    force_exit = getattr(session, "force_exit", None)
    if callable(force_exit):
        try:
            force_exit()
            print(f"[{config_id}] 已强制退出 Fluent")
        except Exception as force_err:
            print(f"[{config_id}] Fluent 强制退出失败: {force_err}")



def _run_metrics_postprocess(args: argparse.Namespace, case_path: str, config_id: int) -> None:
    metrics_script = getattr(args, "metrics_script", None)
    if not metrics_script:
        print(f"[{config_id}] 未配置五项指标后处理脚本，跳过指标计算")
        return
    _require_file(metrics_script, "五项指标后处理脚本")
    compute_script = getattr(args, "compute_metrics_script", None) or os.path.join(
        os.path.dirname(metrics_script),
        "compute_metrics_gen4.py",
    )
    _require_file(compute_script, "指标计算脚本")
    metrics_output_dir = os.path.join(
        args.postprocess_output_dir,
        "metrics",
        f"model_gen4_{config_id}",
    )
    os.makedirs(metrics_output_dir, exist_ok=True)
    command = [
        sys.executable,
        "-u",
        metrics_script,
        "--case-data",
        case_path,
        "--output-dir",
        metrics_output_dir,
        "--compute-script",
        compute_script,
        "--processor-count",
        str(args.metrics_processor_count),
        "--ambient-pressure",
        str(args.metrics_ambient_pressure),
        "--pressure-reference",
        str(args.metrics_pressure_reference),
        "--tcomb",
        str(args.metrics_tcomb),
        "--thrust-axis",
        args.metrics_thrust_axis,
        "--exit-to-throat-area-ratio",
        str(args.metrics_exit_to_throat_area_ratio),
        "--cstar-reference",
        str(args.metrics_cstar_reference),
        "--config-name",
        f"model_gen4_{config_id}",
        "--config-id",
        str(config_id),
    ]
    print(f"[{config_id}] 正在计算五项指标: {metrics_output_dir}")
    subprocess.run(command, check=True)
    print(f"[{config_id}] 五项指标后处理完成: {metrics_output_dir}")
def _write_flag(flag_file: str) -> None:
    parent = os.path.dirname(flag_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_file = f"{flag_file}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write("OK\n")
    os.replace(tmp_file, flag_file)


def main() -> None:
    args = parse_args()
    if args.config_id < 0:
        raise ValueError("参数 config_id 必须是大于等于0的整数。")

    config_id = args.config_id
    case_path = os.path.join(args.case_dir, f"model_gen4_{config_id}.cas.h5")
    data_path = os.path.join(args.case_dir, f"model_gen4_{config_id}.dat.h5")
    _require_file(case_path, "Case 文件")
    _require_file(data_path, "Data 文件")
    _require_file(args.post_journal_path, "后处理 Journal 文件")

    for dir_path in (
        args.postprocess_output_dir,
        args.anim_dir,
        args.working_dir,
        args.working_dir_t,
        args.working_dir_v,
    ):
        os.makedirs(dir_path, exist_ok=True)

    print(f"[配置] 模型编号: {config_id}")
    print(f"[配置] Case 目录: {args.case_dir}")
    print(f"[配置] 后处理 Journal: {args.post_journal_path}")
    print(f"[配置] 额外后处理 Journal: {args.extra_post_journal_path or '<none>'}")
    print(f"[配置] 后处理输出目录: {args.postprocess_output_dir}")

    session = pyfluent.launch_fluent(
        mode=pyfluent.FluentMode.SOLVER,
        precision=pyfluent.Precision.DOUBLE,
        product_version=pyfluent.FluentVersion.v241,
        cleanup_on_exit=True,
        ui_mode="gui",
        cwd=args.working_dir,
        start_watchdog=False,
    )

    try:
        _ensure_session_healthy(session, "启动后")
        print(f"[{config_id}] 正在读取 Case/Data: {case_path}")
        _read_case_data_file(session, case_path)
        _ensure_session_healthy(session, "读取 Case/Data 后")

        print(f"[{config_id}] 正在执行后处理 Journal: {args.post_journal_path}")
        session.tui.file.read_journal(args.post_journal_path)
        _ensure_session_healthy(session, "执行后处理 Journal 后")

        if args.extra_post_journal_path and os.path.isfile(args.extra_post_journal_path):
            print(f"[{config_id}] 正在执行额外后处理 Journal: {args.extra_post_journal_path}")
            session.tui.file.read_journal(args.extra_post_journal_path)
            _ensure_session_healthy(session, "执行额外后处理 Journal 后")
        elif args.extra_post_journal_path:
            print(f"[{config_id}] 额外后处理 Journal 不存在，跳过: {args.extra_post_journal_path}")

        time.sleep(2)
        _move_and_rename(config_id, args.working_dir_t, args.working_dir_v, args.anim_dir)
    except Exception as e:
        print(f"[错误] 后处理模型 {config_id} 时发生异常: {e}")
        raise
    finally:
        print(f"[{config_id}] 正在清理后处理日志文件...")
        _cleanup_log_files(config_id, args.working_dir)
        print(f"[{config_id}] 正在清空后处理工作目录...")
        _cleanup_working_dirs(config_id, [args.working_dir_t, args.working_dir_v])
        _close_session(config_id, session)

    _run_metrics_postprocess(args, case_path, config_id)
    _write_flag(args.flag_file)
    print(f"[{config_id}] 后处理完成标志已写入: {args.flag_file}")


if __name__ == "__main__":
    main()
