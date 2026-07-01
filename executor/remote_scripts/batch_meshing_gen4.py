"""
Fluent Meshing 批处理脚本 - 参数化版本

用法:
    python batch_meshing_gen4.py <config_id> --mpi-bin-dir <path> --workflow-path <path>
        --journal-path <path> --scdoc-dir <path> --output-dir <path>
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import Any

# 强制 Python 使用 UTF-8 编码，避免 conda run 在中文 Windows 上的 GBK 编码崩溃
os.environ["PYTHONUTF8"] = "1"
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]

import ansys.fluent.core as pyfluent


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description='运行 Fluent Meshing 处理指定编号的模型。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    # 必需参数
    parser.add_argument('config_id', type=int, help='模型编号 (必须是大于等于0的整数)')

    # ANSYS 路径参数（必需）
    parser.add_argument('--mpi-bin-dir', type=str, required=True,
                        help='Intel MPI bin 目录路径')
    parser.add_argument('--fluent-path', type=str, required=True,
                        help='Fluent 可执行文件完整路径')

    # 文件路径参数（必需）
    parser.add_argument('--workflow-path', type=str, required=True,
                        help='工作流文件路径 (.wft)')
    parser.add_argument('--journal-path', type=str, required=True,
                        help='Journal 文件路径 (.jou)')

    # 目录路径参数（必需）
    parser.add_argument('--scdoc-dir', type=str, required=True,
                        help='SCDOC 输入目录')
    parser.add_argument('--scdoc-name', type=str, required=True,
                        help='SCDOC 输入文件名')
    parser.add_argument('--output-dir', type=str, required=True,
                        help='网格输出目录')
    parser.add_argument('--working-dir', type=str, required=True,
                        help='Fluent 启动工作目录')

    # Fluent 参数
    parser.add_argument('--processor-count', type=int,
                        required=True,
                        help='处理器核心数')

    return parser.parse_args()


def setup_mpi_environment(mpi_bin_dir: str) -> None:
    """设置 MPI 环境变量。"""
    mpi_root = os.path.dirname(mpi_bin_dir)  # bin 的上级目录即 I_MPI_ROOT
    if not os.path.isdir(mpi_bin_dir):
        raise FileNotFoundError(
            f"MPI bin 目录不存在: {mpi_bin_dir}，"
            f"请确认 --mpi-bin-dir 参数正确。"
        )

    os.environ["I_MPI_ROOT"] = mpi_root
    os.environ["PATH"] = mpi_bin_dir + ";" + os.environ["PATH"]
    print(f"[环境] I_MPI_ROOT = {mpi_root}")


def _require_file(path: str, label: str) -> None:
    """验证 Meshing 启动前必须存在的输入文件。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{label}不存在: {path}")


def _ensure_session_healthy(meshing_session: Any, stage: str) -> None:
    """确认 Fluent server 仍可通过 PyFluent gRPC 通道访问。"""
    try:
        if meshing_session.is_server_healthy():
            print(f"[健康检查] Fluent server 正常: {stage}")
            return
    except Exception as e:
        raise RuntimeError(f"Fluent server 健康检查失败 ({stage}): {e}") from e
    raise RuntimeError(f"Fluent server 健康检查失败 ({stage}): server unhealthy")


def main() -> None:
    """主函数。"""
    args = parse_args()
    config_id = args.config_id

    # 验证参数
    if config_id < 0:
        raise ValueError("参数 config_id 必须是大于等于0的整数。")
    if args.processor_count <= 0:
        raise ValueError("参数 --processor-count 必须是大于0的整数。")
    if os.path.basename(args.scdoc_name) != args.scdoc_name:
        raise ValueError("参数 --scdoc-name 必须是文件名，不能包含路径")

    # 在占用 Fluent 许可证和启动 GUI 前完成输入文件校验。
    import_file_name = os.path.join(args.scdoc_dir, args.scdoc_name)
    _require_file(args.workflow_path, "工作流文件")
    _require_file(args.journal_path, "Journal 文件")
    _require_file(import_file_name, "SCDOC 输入文件")
    os.makedirs(args.working_dir, exist_ok=True)

    # 设置环境变量
    setup_mpi_environment(args.mpi_bin_dir)

    # 打印配置信息
    print(f"[配置] 模型编号: {args.config_id}")
    print(f"[配置] MPI bin 目录: {args.mpi_bin_dir}")
    print(f"[配置] Fluent 可执行文件: {args.fluent_path}")
    print(f"[配置] 工作流文件: {args.workflow_path}")
    print(f"[配置] Journal 文件: {args.journal_path}")
    print(f"[配置] SCDOC 目录: {args.scdoc_dir}")
    print(f"[配置] 输出目录: {args.output_dir}")
    print(f"[配置] Fluent 工作目录: {args.working_dir}")
    print(f"[配置] 处理器核心数: {args.processor_count}")

    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)

    # 启动 Fluent Meshing 模式
    print("[启动] 正在启动 Fluent Meshing...")
    meshing_session = pyfluent.launch_fluent(
        mode=pyfluent.FluentMode.MESHING,
        precision=pyfluent.Precision.DOUBLE,
        processor_count=args.processor_count,
        product_version=pyfluent.FluentVersion.v241,
        fluent_path=args.fluent_path,
        cleanup_on_exit=True,
        ui_mode="gui",
        cwd=args.working_dir,
        start_watchdog=False,
    )

    # 临时文件路径（用于 finally 清理）
    temp_wft: str | None = None
    temp_jou: str | None = None

    try:
        _ensure_session_healthy(meshing_session, "启动后")

        # 1. 构建文件路径
        print(f"[{config_id}] 输入文件: {import_file_name}")

        # 2. 创建临时工作流文件副本，替换构型号占位符
        #    避免就地修改共享的 wft 文件导致状态污染（崩溃后 {config} 已被替换）
        scripts_dir = os.path.dirname(args.workflow_path)
        temp_wft = os.path.join(scripts_dir, f"meshing_gen4_{config_id}.wft")
        print(f"[{config_id}] 正在创建工作流副本: {temp_wft}")
        shutil.copy2(args.workflow_path, temp_wft)
        with open(temp_wft, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('{config}', str(config_id))
        with open(temp_wft, 'w', encoding='utf-8') as f:
            f.write(content)

        # 3. 创建临时 Journal 文件，引用临时工作流文件
        temp_jou = os.path.join(scripts_dir, f"meshing_gen4_{config_id}.jou")
        print(f"[{config_id}] 正在创建 Journal 副本: {temp_jou}")
        with open(args.journal_path, 'r', encoding='utf-8') as f:
            jou_content = f.read()
        jou_content = jou_content.replace(
            'meshing_gen4.wft',
            f'meshing_gen4_{config_id}.wft'
        )
        with open(temp_jou, 'w', encoding='utf-8') as f:
            f.write(jou_content)

        # 4. 执行临时 Journal 文件
        _ensure_session_healthy(meshing_session, "执行 Journal 前")
        print(f"[{config_id}] 正在执行 Journal 文件: {temp_jou}")
        meshing_session.tui.file.read_journal(temp_jou)
        _ensure_session_healthy(meshing_session, "执行 Journal 后")

        # 5. 保存网格
        mesh_file_name = f"model_gen4_{config_id}.msh.h5"
        mesh_full_path = os.path.join(args.output_dir, mesh_file_name)
        print(f"[{config_id}] 正在保存网格: {mesh_full_path}")
        meshing_session.tui.file.write_mesh(mesh_full_path)
        print(f"[{config_id}] 网格已保存到 {mesh_full_path}")

        time.sleep(2)

        # 5. 清理缓存文件夹
        folder_name = f"model_gen4_{config_id}_workflow_files"
        folder_path = os.path.join(args.output_dir, folder_name)
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
        raise

    finally:
        # ★ finally 确保无论成功/异常都执行临时文件清理和 Fluent 退出
        for temp_file in (temp_wft, temp_jou):
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    print(f"[{config_id}] 已清理临时文件: {os.path.basename(temp_file)}")
                except Exception as cleanup_err:
                    print(f"[{config_id}] 清理临时文件失败: {cleanup_err}")

        try:
            meshing_session.exit()
        except Exception as cleanup_err:
            print(f"[{config_id}] Fluent 退出失败: {cleanup_err}")

    print(f"模型 {config_id} 的网格生成完成！")


if __name__ == "__main__":
    main()
