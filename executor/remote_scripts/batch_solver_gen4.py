"""
Fluent Solver 批处理脚本 - 参数化版本

用法:
    python batch_solver_gen4.py <config_id> [options]

示例:
    python batch_solver_gen4.py 5
    python batch_solver_gen4.py 5 --msh-dir "D:\\custom\\msh" --output-dir "D:\\custom\\case"
"""

import ansys.fluent.core as pyfluent
import os
import shutil
import time
import argparse


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description='运行 Fluent Solver 处理指定编号的模型。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    # 必需参数
    parser.add_argument('config_id', type=int, help='模型编号 (必须是大于0的整数)')

    # ANSYS 路径参数
    parser.add_argument('--ansys-root', type=str,
                        default=r"C:\Program Files\ANSYS Inc\v241",
                        help='ANSYS 安装根目录 (默认: C:\\Program Files\\ANSYS Inc\\v241)')

    # 文件路径参数
    parser.add_argument('--journal-path', type=str,
                        default=None,
                        help='求解 Journal 文件路径 (.jou) (默认: <remote_root>/solver_gen4.jou)')
    parser.add_argument('--post-journal-path', type=str,
                        default=None,
                        help='后处理 Journal 文件路径 (.jou) (默认: <remote_root>/solver_post_gen4.jou)')

    # 目录路径参数
    parser.add_argument('--remote-root', type=str,
                        default=r"D:\xkz_1020",
                        help='远程工作根目录 (默认: D:\\xkz_1020)')
    parser.add_argument('--msh-dir', type=str,
                        default=None,
                        help='网格输入目录 (默认: <remote_root>/msh)')
    parser.add_argument('--output-dir', type=str,
                        default=None,
                        help='算例输出目录 (默认: <remote_root>/case)')
    parser.add_argument('--anim-dir', type=str,
                        default=None,
                        help='动画输出目录 (默认: <remote_root>/animation)')
    parser.add_argument('--working-dir-t', type=str,
                        default=None,
                        help='温度动画工作目录 (默认: <remote_root>/workingdir/animation-t)')
    parser.add_argument('--working-dir-v', type=str,
                        default=None,
                        help='速度动画工作目录 (默认: <remote_root>/workingdir/animation-v)')

    # Fluent 参数
    parser.add_argument('--processor-count', type=int,
                        default=128,
                        help='处理器核心数 (默认: 128)')
    parser.add_argument('--iterate-count', type=int,
                        default=1000,
                        help='迭代次数 (默认: 1000)')

    return parser.parse_args()


def setup_ansys_environment(ansys_root: str):
    """设置 ANSYS 环境变量。"""
    fluent_root = os.path.join(ansys_root, "fluent")
    # 查找 Fluent 版本目录（取最新版本）
    if os.path.exists(fluent_root):
        versions = [d for d in os.listdir(fluent_root) if d.startswith("fluent")]
        if versions:
            versions.sort(reverse=True)  # 降序排列，取最新版本
            fluent_version_dir = os.path.join(fluent_root, versions[0])
            mpi_root = os.path.join(fluent_version_dir, "multiport", "mpi", "win64", "intel2021")
            if os.path.exists(mpi_root):
                os.environ["I_MPI_ROOT"] = mpi_root
                os.environ["PATH"] = mpi_root + "\\bin;" + os.environ["PATH"]
                print(f"[环境] I_MPI_ROOT = {mpi_root}")
                return

    # 如果自动查找失败，使用传统路径
    mpi_root = os.path.join(ansys_root, "fluent", "fluent24.1.0", "multiport", "mpi", "win64", "intel2021")
    os.environ["I_MPI_ROOT"] = mpi_root
    os.environ["PATH"] = mpi_root + "\\bin;" + os.environ["PATH"]
    print(f"[环境] I_MPI_ROOT = {mpi_root}")

    # 设置 MPI 绑定参数
    os.environ["I_MPI_PIN"] = "1"
    os.environ["I_MPI_PIN_DOMAIN"] = "numa"
    os.environ["I_MPI_PIN_ORDER"] = "compact"
    os.environ["I_MPI_PIN_PROCESSOR_LIST"] = "64-127"
    os.environ["I_MPI_DEBUG"] = "5"


def move_and_rename(config_id: int, working_dir_t: str, working_dir_v: str, anim_dir: str):
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


def cleanup_working_dirs(config_id: int, working_dirs: list):
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


def cleanup_log_files(config_id: int, output_dir: str):
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


def main():
    """主函数。"""
    args = parse_args()

    # 验证参数
    if args.config_id <= 0:
        raise ValueError("参数 config_id 必须是一个大于0的整数。")

    # 设置环境变量
    setup_ansys_environment(args.ansys_root)

    # 解析路径参数（使用默认值或用户指定值）
    remote_root = args.remote_root
    journal_path = args.journal_path or os.path.join(remote_root, "solver_gen4.jou")
    post_journal_path = args.post_journal_path or os.path.join(remote_root, "solver_post_gen4.jou")
    msh_dir = args.msh_dir or os.path.join(remote_root, "msh")
    output_dir = args.output_dir or os.path.join(remote_root, "case")
    anim_dir = args.anim_dir or os.path.join(remote_root, "animation")
    working_dir_t = args.working_dir_t or os.path.join(remote_root, "workingdir", "animation-t")
    working_dir_v = args.working_dir_v or os.path.join(remote_root, "workingdir", "animation-v")

    # 打印配置信息
    print(f"[配置] 模型编号: {args.config_id}")
    print(f"[配置] ANSYS 根目录: {args.ansys_root}")
    print(f"[配置] 求解 Journal: {journal_path}")
    print(f"[配置] 后处理 Journal: {post_journal_path}")
    print(f"[配置] 网格目录: {msh_dir}")
    print(f"[配置] 输出目录: {output_dir}")
    print(f"[配置] 动画目录: {anim_dir}")
    print(f"[配置] 工作目录 T: {working_dir_t}")
    print(f"[配置] 工作目录 V: {working_dir_v}")
    print(f"[配置] 处理器核心数: {args.processor_count}")
    print(f"[配置] 迭代次数: {args.iterate_count}")

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

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
        import_file_name = os.path.join(msh_dir, f"model_gen4_{config_id}.msh.h5")
        if not os.path.exists(import_file_name):
            print(f"错误：未找到网格文件 {import_file_name}")
            solver_session.exit()
            exit(1)

        print(f"[{config_id}] 正在读取网格文件: {import_file_name}")
        solver_session.tui.file.read_case(import_file_name)

        # 6.2 执行 journal
        print(f"[{config_id}] 正在执行求解 Journal: {journal_path}")
        solver_session.tui.file.read_journal(journal_path)

        # 6.3 启动仿真
        print(f"[{config_id}] 正在启动仿真迭代 (共 {args.iterate_count} 步)...")
        solver_session.tui.solve.iterate(args.iterate_count)

        # 6.4 执行后处理 journal
        print(f"[{config_id}] 正在执行后处理 Journal: {post_journal_path}")
        solver_session.tui.file.read_journal(post_journal_path)

        time.sleep(2)
        move_and_rename(config_id, working_dir_t, working_dir_v, anim_dir)

        # 6.5 保存算例
        case_file_name = f"model_gen4_{config_id}.cas.h5"
        case_full_path = os.path.join(output_dir, case_file_name)
        print(f"[{config_id}] 正在保存算例: {case_full_path}")
        solver_session.tui.file.write_case_data(case_full_path)
        print(f"[{config_id}] 算例已保存到 {case_full_path}")

    except Exception as e:
        print(f"[错误] 处理模型 {config_id} 时发生异常: {e}")
        solver_session.exit()
        exit(1)

    # --- 7. 最后的清理工作 (日志 + 工作目录缓存) ---
    print(f"[{config_id}] 正在清理日志文件...")
    cleanup_log_files(config_id, output_dir)

    print(f"[{config_id}] 正在清空工作目录...")
    cleanup_working_dirs(config_id, [working_dir_t, working_dir_v])

    # --- 8. 退出 Fluent ---
    solver_session.exit()
    print(f"模型 {config_id} 的仿真计算完成！")


if __name__ == "__main__":
    main()
