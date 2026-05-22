"""
Fluent Meshing 批处理脚本 - 参数化版本

用法:
    python batch_meshing_gen4.py <config_id> [options]

示例:
    python batch_meshing_gen4.py 5
    python batch_meshing_gen4.py 5 --scdoc-dir "D:\\custom\\scdoc" --output-dir "D:\\custom\\msh"
"""

import ansys.fluent.core as pyfluent
import json
import os
import time
import shutil
import argparse


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description='运行 Fluent Meshing 处理指定编号的模型。',
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
    parser.add_argument('--workflow-path', type=str,
                        default=None,
                        help='工作流文件路径 (.wft) (默认: <remote_root>/meshing_gen4.wft)')
    parser.add_argument('--journal-path', type=str,
                        default=None,
                        help='Journal 文件路径 (.jou) (默认: <remote_root>/meshing_gen4.jou)')

    # 目录路径参数
    parser.add_argument('--remote-root', type=str,
                        default=r"D:\xkz_1020",
                        help='远程工作根目录 (默认: D:\\xkz_1020)')
    parser.add_argument('--scdoc-dir', type=str,
                        default=None,
                        help='SCDOC 输入目录 (默认: <remote_root>/scdoc)')
    parser.add_argument('--output-dir', type=str,
                        default=None,
                        help='网格输出目录 (默认: <remote_root>/msh)')

    # Fluent 参数
    parser.add_argument('--processor-count', type=int,
                        default=64,
                        help='处理器核心数 (默认: 64)')

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
    workflow_path = args.workflow_path or os.path.join(remote_root, "meshing_gen4.wft")
    journal_path = args.journal_path or os.path.join(remote_root, "meshing_gen4.jou")
    scdoc_dir = args.scdoc_dir or os.path.join(remote_root, "scdoc")
    output_dir = args.output_dir or os.path.join(remote_root, "msh")

    # 打印配置信息
    print(f"[配置] 模型编号: {args.config_id}")
    print(f"[配置] ANSYS 根目录: {args.ansys_root}")
    print(f"[配置] 工作流文件: {workflow_path}")
    print(f"[配置] Journal 文件: {journal_path}")
    print(f"[配置] SCDOC 目录: {scdoc_dir}")
    print(f"[配置] 输出目录: {output_dir}")
    print(f"[配置] 处理器核心数: {args.processor_count}")

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 启动 Fluent Meshing 模式
    print("[启动] 正在启动 Fluent Meshing...")
    meshing_session = pyfluent.launch_fluent(
        mode=pyfluent.FluentMode.MESHING,
        precision=pyfluent.Precision.DOUBLE,
        processor_count=args.processor_count,
        product_version=pyfluent.FluentVersion.v241,
        cleanup_on_exit=True,
        ui_mode="gui",
        env={"lang": "zh"}
    )

    config_id = args.config_id

    try:
        # 1. 构建文件路径
        import_file_name = os.path.join(scdoc_dir, f"model_gen4_{config_id}.scdoc")
        print(f"[{config_id}] 输入文件: {import_file_name}")

        # 2. 修改工作流 JSON 文件中的路径
        print(f"[{config_id}] 正在修改工作流文件: {workflow_path}")
        with open(workflow_path, 'r', encoding='utf-8') as f:
            workflow_data = json.load(f)

        root_tasks = workflow_data.get('workflow', {}).get('ROOT', {})
        for key, task in root_tasks.items():
            if task.get('CommandName', '') == 'ImportGeometry':
                task['Arguments']['FileName'] = import_file_name
                break

        with open(workflow_path, 'w', encoding='utf-8') as f:
            json.dump(workflow_data, f, indent=4)

        # 3. 执行 Journal 文件
        print(f"[{config_id}] 正在执行 Journal 文件: {journal_path}")
        meshing_session.tui.file.read_journal(journal_path)

        # 4. 保存网格
        mesh_file_name = f"model_gen4_{config_id}.msh.h5"
        mesh_full_path = os.path.join(output_dir, mesh_file_name)
        print(f"[{config_id}] 正在保存网格: {mesh_full_path}")
        meshing_session.tui.file.write_mesh(mesh_full_path)
        print(f"[{config_id}] 网格已保存到 {mesh_full_path}")

        time.sleep(2)

        # 5. 清理缓存文件夹
        folder_name = f"model_gen4_{config_id}_workflow_files"
        folder_path = os.path.join(output_dir, folder_name)
        if os.path.exists(folder_path):
            try:
                shutil.rmtree(folder_path)
                print(f"[{config_id}] 已删除缓存文件夹: {folder_path}")
            except Exception as e:
                print(f"[{config_id}] 删除缓存文件夹失败: {e}")
        else:
            print(f"[{config_id}] 未找到缓存文件夹: {folder_path}")

    except Exception as e:
        print(f"[错误] 处理模型 {config_id} 时发生异常: {e}")
        meshing_session.exit()
        exit(1)

    # 6. 退出 Fluent
    meshing_session.exit()
    print(f"模型 {config_id} 的网格生成完成！")


if __name__ == "__main__":
    main()
