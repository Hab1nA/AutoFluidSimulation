import ansys.fluent.core as pyfluent
from ansys.fluent.core.utils.file_transfer_service import StandaloneFileTransferStrategy
import json
import os
import time
import shutil
import argparse # 导入参数解析模块

# --- 1. 设置环境变量 ---
os.environ["I_MPI_ROOT"] = r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021"
os.environ["PATH"] = (r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin" + os.environ["PATH"])

# --- 2. 定义参数解析器 ---
parser = argparse.ArgumentParser(description='运行 Fluent Meshing 处理指定编号的模型。')
parser.add_argument('time_location', type=int, help='模型编号 (必须是大于0的整数)')
args = parser.parse_args()

# 确保输入的数字大于0
if args.time_location <= 0:
    raise ValueError("参数 time_location 必须是一个大于0的整数。")

# --- 3. 启动 Fluent Meshing 模式 ---
meshing_session = pyfluent.launch_fluent(
    mode=pyfluent.FluentMode.MESHING,
    precision=pyfluent.Precision.DOUBLE,
    processor_count=64,
    product_version=pyfluent.FluentVersion.v241,
    cleanup_on_exit=True,
    ui_mode="gui",
    env={"lang": "zh"}
)

# --- 4. 文件路径定义 ---
saved_workflow_path = r"D:\xkz_1020\meshing_gen4.wft"
saved_journal_path = r"D:\xkz_1020\meshing_gen4.jou"
scdoc_dir = r"D:\xkz_1020\scdoc"
output_dir = r"D:\xkz_1020\msh"

os.makedirs(output_dir, exist_ok=True)

# --- 5. 核心处理逻辑 (单次执行) ---
i = args.time_location # 将参数赋值给循环变量 i

# 1. 构建文件路径
import_file_name = os.path.join(scdoc_dir, f"model_gen4_{i}.scdoc")

# 2. 修改工作流 JSON 文件中的路径
try:
    with open(saved_workflow_path, 'r', encoding='utf-8') as f:
        workflow_data = json.load(f)

    root_tasks = workflow_data.get('workflow', {}).get('ROOT', {})
    for key, task in root_tasks.items():
        if task.get('CommandName', '') == 'ImportGeometry':
            task['Arguments']['FileName'] = import_file_name
            break

    with open(saved_workflow_path, 'w', encoding='utf-8') as f:
        json.dump(workflow_data, f, indent=4)

    # 3. 执行 Journal 文件
    meshing_session.tui.file.read_journal(saved_journal_path)

    # 4. 保存网格
    mesh_file_name = f"model_gen4_{i}.msh.h5"
    mesh_full_path = os.path.join(output_dir, mesh_file_name)
    meshing_session.tui.file.write_mesh(mesh_full_path)
    print(f"[{i}] 网格已保存到 {mesh_full_path}")

    time.sleep(2)

    # 5. 清理缓存文件夹
    folder_name = f"model_gen4_{i}_workflow_files"
    folder_path = os.path.join(output_dir, folder_name)
    if os.path.exists(folder_path):
        try:
            shutil.rmtree(folder_path)
            print(f"[{i}] 已删除缓存文件夹: {folder_path}")
        except Exception as e:
            print(f"[{i}] 删除缓存文件夹失败: {e}")
    else:
        print(f"[{i}] 未找到缓存文件夹: {folder_path}")

except Exception as e:
    print(f"[错误] 处理模型 {i} 时发生异常: {e}")
    meshing_session.exit()
    exit(1)

# --- 6. 退出 Fluent ---
meshing_session.exit()
print(f"模型 {i} 的网格生成完成！")