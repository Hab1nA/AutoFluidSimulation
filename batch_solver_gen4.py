import ansys.fluent.core as pyfluent
from ansys.fluent.core.utils.data_transfer import transfer_case
import os
import shutil
import time
os.environ["I_MPI_ROOT"] = r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021"
os.environ["PATH"] = (
    r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin"
    + os.environ["PATH"]
)
os.environ["I_MPI_PIN"] = "1"
os.environ["I_MPI_PIN_DOMAIN"] = "numa"
os.environ["I_MPI_PIN_ORDER"] = "compact"
os.environ["I_MPI_PIN_PROCESSOR_LIST"] = "64-127"
os.environ["I_MPI_DEBUG"] = "5"


# 1. 启动 Fluent Solver 模式
# 设置版本为 2024R1 (v241)，并启用 64 个 CPU 核心
solver_session = pyfluent.launch_fluent(
    mode=pyfluent.FluentMode.SOLVER,
    precision=pyfluent.Precision.DOUBLE,
    processor_count=128, # NUMA架构的EPYC类CPU上过多的CPU核心可能会导致性能下降
    product_version=pyfluent.FluentVersion.v241,
    cleanup_on_exit=True,
    #ui_mode="gui",
    #env={"lang": "zh"}
)

saved_journal_path = r"D:\xkz_1020\solver_gen4.jou"
saved_post_journal_path = r"D:\xkz_1020\solver_post_gen4.jou"
msh_dir = r"D:\xkz_1020\msh"
output_dir = r"D:\xkz_1020\case"
anim_dir = r"D:\xkz_1020\animation"
os.makedirs(output_dir, exist_ok=True)
files_to_delete = [
        "report-def-p-rfile.out",
        "report-def-v-rfile.out",
        "report-def-t-rfile.out",
    ]

def move_and_rename(i):
    files = [
        ("animation-v.mp4", f"v_gen4_{i}.mp4"),
        ("animation-t.mp4", f"t_gen4_{i}.mp4"),
    ]
    
    for src_name, dst_name in files:
        src_path = os.path.join(msh_dir, src_name)
        dst_path = os.path.join(anim_dir, dst_name)

        if os.path.exists(src_path):
            # 如果目标文件已存在，先删除（避免 move 报错）
            if os.path.exists(dst_path):
                os.remove(dst_path)

            try:
                shutil.move(src_path, dst_path)
                print(f"[{i}] 移动动画: {src_name} -> {dst_name}")
            except Exception as e:
                print(f"[{i}] 移动动画失败: {src_name}, error: {e}")
        else:
            print(f"[{i}] 未找到动画: {src_name}")

for i in range(1, 101):

    # 输入 msh 文件
    import_file_name = os.path.join(msh_dir, f"model_gen4_{i}.msh.h5")
    solver_session.tui.file.read_case(import_file_name)  # type: ignore
    
    # 执行 journal
    solver_session.tui.file.read_journal(saved_journal_path)  # type: ignore

    # 启动仿真
    solver_session.tui.solve.iterate(500) # type: ignore
    
    # 执行 后处理journal
    solver_session.tui.file.read_journal(saved_post_journal_path)  # type: ignore
    time.sleep(2)
    move_and_rename(i)

    # 保存网格
    case_file_name = f"model_gen4_{i}.cas.h5"
    case_full_path = os.path.join(output_dir, case_file_name)
    solver_session.tui.file.write_case_data(case_full_path) # type: ignore
    
    print(f"[{i}] 算例已保存到 {case_full_path}")

    for f in files_to_delete:
        path = os.path.join(output_dir, f)
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"[{i}] 已删除日志: {f}")
            except Exception as e:
                print(f"[{i}] 删除日志失败 {f}: {e}")
        else:
            print(f"[{i}] 未找到日志: {f}")

solver_session.exit() # type: ignore
print("批量算例生成完成！")