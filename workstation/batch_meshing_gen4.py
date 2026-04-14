import ansys.fluent.core as pyfluent
from ansys.fluent.core.utils.file_transfer_service import StandaloneFileTransferStrategy
import json
import os
import time
import shutil
os.environ["I_MPI_ROOT"] = r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021"
os.environ["PATH"] = (
    r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin"
    + os.environ["PATH"]
)

# 1. 启动 Fluent Meshing 模式
# 设置版本为 2024R1 (v241)，并启用 16 个 CPU 核心
meshing_session = pyfluent.launch_fluent(
    mode=pyfluent.FluentMode.MESHING,
    precision=pyfluent.Precision.DOUBLE,
    processor_count=64, # NUMA架构的EPYC类CPU上过多的CPU核心可能会导致性能下降
    product_version=pyfluent.FluentVersion.v241,
    cleanup_on_exit=True,
    ui_mode="gui",
    env={"lang": "zh"}
    
)
# 设置MPI类型为intel，避免默认type在NUMA架构的EPYC类CPU的系统上出现性能问题
#meshing_session.tui.parametric_study.update.concurrent.set.mpi_type("intel") # type: ignore

# 2. 加载工作流文件
# 我他妈简直是个天才，通过预先将模型路径写进工作流文件里，避免了读取工作流后没办法成功执行Import Geometry任务的尴尬局面。我他妈想了几个小时都没办法解决Import Geometry任务无法执行的问题，现在让我们直接绕过这个bug。
saved_workflow_path = r"D:\xkz_1020\meshing_gen4.wft"
saved_journal_path = r"D:\xkz_1020\meshing_gen4.jou"
scdoc_dir = r"D:\xkz_1020\scdoc"
output_dir = r"D:\xkz_1020\msh"
os.makedirs(output_dir, exist_ok=True)

for i in range(1, 101):

    # 输入 scdoc 文件
    import_file_name = os.path.join(scdoc_dir, f"model_gen4_{i}.scdoc")
    
    # 读取 workflow 文件
    with open(saved_workflow_path, 'r', encoding='utf-8') as f:
        workflow_data = json.load(f)
    
    # 修改 ImportGeometry 的路径
    root_tasks = workflow_data.get('workflow', {}).get('ROOT', {})
    for key, task in root_tasks.items():
        if task.get('CommandName', '') == 'ImportGeometry':
            task['Arguments']['FileName'] = import_file_name
            break
    
    # 保存回 workflow 文件
    with open(saved_workflow_path, 'w', encoding='utf-8') as f:
        json.dump(workflow_data, f, indent=4)
    
    # 执行 journal
    meshing_session.tui.file.read_journal(saved_journal_path)  # type: ignore

    # 调试用，直接读取一个msh文件，看看能不能成功执行后续的保存网格命令
    #test_msh_path = fr"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\resource\Combustion_zone_model.35_{i}.msh.gz"
    #meshing_session.tui.file.read_mesh(test_msh_path)
    
    # 保存网格
    mesh_file_name = f"model_gen4_{i}.msh.h5"
    mesh_full_path = os.path.join(output_dir, mesh_file_name)
    meshing_session.tui.file.write_mesh(mesh_full_path) # type: ignore
    
    print(f"[{i}] 网格已保存到 {mesh_full_path}")

    time.sleep(2)
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


meshing_session.exit() # type: ignore
print("批量网格生成完成！")