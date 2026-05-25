"""
Fluent Solver 批处理脚本 - 参数化版本

用法:
    python batch_solver_gen4.py <config_id> --mpi-bin-dir <path> --journal-path <path>
        --post-journal-path <path> --msh-dir <path> --output-dir <path>
        --anim-dir <path> --working-dir-t <path> --working-dir-v <path>
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

import ansys.fluent.core as pyfluent


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description='运行 Fluent Solver 处理指定编号的模型。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    # 必需参数
    parser.add_argument('config_id', type=int, help='模型编号 (必须是大于等于0的整数)')

    # ANSYS 路径参数（必需）
    parser.add_argument('--mpi-bin-dir', type=str, required=True,
                        help='Intel MPI bin 目录路径')

    # 文件路径参数（必需）
    parser.add_argument('--journal-path', type=str, required=True,
                        help='求解 Journal 文件路径 (.jou)')
    parser.add_argument('--post-journal-path', type=str, required=True,
                        help='后处理 Journal 文件路径 (.jou)')

    # 目录路径参数（必需）
    parser.add_argument('--msh-dir', type=str, required=True,
                        help='网格输入目录')
    parser.add_argument('--output-dir', type=str, required=True,
                        help='算例输出目录')
    parser.add_argument('--anim-dir', type=str, required=True,
                        help='动画输出目录')
    parser.add_argument('--working-dir-t', type=str, required=True,
                        help='温度动画工作目录')
    parser.add_argument('--working-dir-v', type=str, required=True,
                        help='速度动画工作目录')

    # Fluent 参数
    parser.add_argument('--processor-count', type=int,
                        default=128,
                        help='处理器核心数 (默认: 128)')
    parser.add_argument('--iterate-count', type=int,
                        default=1000,
                        help='迭代次数 (默认: 1000)')

    return parser.parse_args()


def setup_mpi_environment(mpi_bin_dir: str) -> None:
    """设置 MPI 环境变量（含 MPI 绑定参数）。"""
    mpi_root = os.path.dirname(mpi_bin_dir)  # bin 的上级目录即 I_MPI_ROOT
    if not os.path.isdir(mpi_bin_dir):
        raise FileNotFoundError(
            f"MPI bin 目录不存在: {mpi_bin_dir}，"
            f"请确认 --mpi-bin-dir 参数正确。"
        )

    os.environ["I_MPI_ROOT"] = mpi_root
    os.environ["PATH"] = mpi_bin_dir + ";" + os.environ["PATH"]
    os.environ["PATH"] = mpi_root + "\\bin;" + os.environ["PATH"]
    print(f"[环境] I_MPI_ROOT = {mpi_root}")

    # 设置 MPI 绑定参数
    os.environ["I_MPI_PIN"] = "1"
    os.environ["I_MPI_PIN_DOMAIN"] = "numa"
    os.environ["I_MPI_PIN_ORDER"] = "compact"
    os.environ["I_MPI_PIN_PROCESSOR_LIST"] = "64-127"
    os.environ["I_MPI_DEBUG"] = "5"


def move_and_rename(config_id: int, working_dir_t: str, working_dir_v: str, anim_dir: str) -> None:
    """移动并重命名动画文件。"""
    files = [
        (os.path.join(working_dir_v, "animation-v.mp4"), f"v_gen4_{config_id}.mp4"),
        (os.path.join(working_dir_t, "animation-t.mp4"), f"t_gen4_{config_id}.mp4"),
    ]

    for src_path, dst_name in files:
        dst_path = os.path.join(anim_dir, dst_name)

        if os.path.exists(src_path):
            # 如果目标文件已存在，先删除（避免 move 报错）
            if os.path.exists(dst_path):
                os.remove(dst_path)
            try:
                shutil.move(src_path, dst_path)
                print(f"[{config_id}] 移动动画: {src_path} -> {dst_path}")
            except Exception as e:
                print(f"[{config_id}] 移动动画失败: {src_path}, error: {e}")
        else:
            print(f"[{config_id}] 未找到动画: {src_path}")


def cleanup_working_dirs(config_id: int, working_dirs: list[str]) -> None:
    """清空工作目录下的所有内容。"""
    for dir_path in working_dirs:
        if os.path.exists(dir_path):
            try:
                # 遍历文件夹下的所有文件和子文件夹并删除
                for filename in os.listdir(dir_path):
                    file_path = os.path.join(dir_path, filename)
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)  # 删除文件或链接
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)  # 删除子文件夹
                print(f"[{config_id}] 已清空目录内容: {dir_path}")
            except Exception as e:
                print(f"[{config_id}] 清空目录失败 {dir_path}: {e}")
        else:
            print(f"[{config_id}] 目录不存在，无需清空: {dir_path}")


def cleanup_log_files(config_id: int, output_dir: str) -> None:
    """清理日志文件。"""
    files_to_delete = [
        "report-def-p-rfile.out",
        "report-def-v-rfile.out",
        "report-def-t-rfile.out",
    ]

    for f in files_to_delete:
        path = os.path.join(output_dir, f)
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"[{config_id}] 已删除日志: {f}")
            except Exception as e:
                print(f"[{config_id}] 删除日志失败 {f}: {e}")
        else:
            print(f"[{config_id}] 未找到日志: {f}")


def main() -> None:
    """主函数。"""
    args = parse_args()

    # 验证参数
    if args.config_id < 0:
        raise ValueError("参数 config_id 必须是大于等于0的整数。")

    # 设置环境变量
    setup_mpi_environment(args.mpi_bin_dir)

    # 打印配置信息
    print(f"[配置] 模型编号: {args.config_id}")
    print(f"[配置] MPI bin 目录: {args.mpi_bin_dir}")
    print(f"[配置] 求解 Journal: {args.journal_path}")
    print(f"[配置] 后处理 Journal: {args.post_journal_path}")
    print(f"[配置] 网格目录: {args.msh_dir}")
    print(f"[配置] 输出目录: {args.output_dir}")
    print(f"[配置] 动画目录: {args.anim_dir}")
    print(f"[配置] 工作目录 T: {args.working_dir_t}")
    print(f"[配置] 工作目录 V: {args.working_dir_v}")
    print(f"[配置] 处理器核心数: {args.processor_count}")
    print(f"[配置] 迭代次数: {args.iterate_count}")

    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)

    # 启动 Fluent Solver 模式
    print("[启动] 正在启动 Fluent Solver...")
    solver_session = pyfluent.launch_fluent(
        mode=pyfluent.FluentMode.SOLVER,
        precision=pyfluent.Precision.DOUBLE,
        processor_count=args.processor_count,
        product_version=pyfluent.FluentVersion.v241,
        cleanup_on_exit=True,
    )

    config_id = args.config_id

    try:
        # 6.1 读取网格文件
        import_file_name = os.path.join(args.msh_dir, f"model_gen4_{config_id}.msh.h5")
        if not os.path.exists(import_file_name):
            print(f"错误：未找到网格文件 {import_file_name}")
            solver_session.exit()
            sys.exit(1)

        print(f"[{config_id}] 正在读取网格文件: {import_file_name}")
        solver_session.tui.file.read_case(import_file_name)

        # 6.2 执行 journal
        print(f"[{config_id}] 正在执行求解 Journal: {args.journal_path}")
        solver_session.tui.file.read_journal(args.journal_path)

        # 6.3 启动仿真
        print(f"[{config_id}] 正在启动仿真迭代 (共 {args.iterate_count} 步)...")
        solver_session.tui.solve.iterate(args.iterate_count)

        # 6.4 执行后处理 journal
        print(f"[{config_id}] 正在执行后处理 Journal: {args.post_journal_path}")
        solver_session.tui.file.read_journal(args.post_journal_path)

        time.sleep(2)
        move_and_rename(config_id, args.working_dir_t, args.working_dir_v, args.anim_dir)

        # 6.5 保存算例
        case_file_name = f"model_gen4_{config_id}.cas.h5"
        case_full_path = os.path.join(args.output_dir, case_file_name)
        print(f"[{config_id}] 正在保存算例: {case_full_path}")
        solver_session.tui.file.write_case_data(case_full_path)
        print(f"[{config_id}] 算例已保存到 {case_full_path}")

    except Exception as e:
        print(f"[错误] 处理模型 {config_id} 时发生异常: {e}")
        solver_session.exit()
        sys.exit(1)

    # --- 7. 最后的清理工作 (日志 + 工作目录缓存) ---
    print(f"[{config_id}] 正在清理日志文件...")
    cleanup_log_files(config_id, args.output_dir)

    print(f"[{config_id}] 正在清空工作目录...")
    cleanup_working_dirs(config_id, [args.working_dir_t, args.working_dir_v])

    # --- 8. 退出 Fluent ---
    solver_session.exit()
    print(f"模型 {config_id} 的仿真计算完成！")


if __name__ == "__main__":
    main()
