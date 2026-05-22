import ansys.fluent.core as pyfluent
from ansys.fluent.core.utils.data_transfer import transfer_case
import os
import shutil
import time
import argparse  # 导入参数解析模块

# --- 1. 设置环境变量 ---
os.environ["I_MPI_ROOT"] = r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021"
os.environ["PATH"] = (r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin" + os.environ["PATH"])
os.environ["I_MPI_PIN"] = "1"
os.environ["I_MPI_PIN_DOMAIN"] = "numa"
os.environ["I_MPI_PIN_ORDER"] = "compact"
os.environ["I_MPI_PIN_PROCESSOR_LIST"] = "64-127"
os.environ["I_MPI_DEBUG"] = "5"

# --- 2. 定义参数解析器 ---
parser = argparse.ArgumentParser(description='运行 Fluent Solver 处理指定编号的模型。')
parser.add_argument('time_location', type=int, help='模型编号 (必须是大于0的整数)')
args = parser.parse_args()

# 确保输入的数字大于0
if args.time_location <= 0:
    raise ValueError("参数 time_location 必须是一个大于0的整数。")

# --- 3. 启动 Fluent Solver 模式 ---
solver_session = pyfluent.launch_fluent(
    mode=pyfluent.FluentMode.SOLVER,
    precision=pyfluent.Precision.DOUBLE,
    processor_count=128, 
    product_version=pyfluent.FluentVersion.v241,
    cleanup_on_exit=True,
    # ui_mode="gui", 
    # env={"lang": "zh"}
)

# --- 4. 文件路径定义 ---
saved_journal_path = r"D:\xkz_1020\solver_gen4.jou"
saved_post_journal_path = r"D:\xkz_1020\solver_post_gen4.jou"
msh_dir = r"D:\xkz_1020\msh"
output_dir = r"D:\xkz_1020\case"
anim_dir = r"D:\xkz_1020\animation"
# 定义工作目录路径
working_anim_t = r"D:\xkz_1020\workingdir\animation-t"
working_anim_v = r"D:\xkz_1020\workingdir\animation-v"

os.makedirs(output_dir, exist_ok=True)

# --- 5. 定义文件移动函数 (已修改源路径) ---
def move_and_rename(i):
    # 修改逻辑：源路径分别指向 working_anim_t 和 working_anim_v
    # 假设生成的文件名是 animation-t.mp4 和 animation-v.mp4
    files = [
        (os.path.join(working_anim_v, "animation-v.mp4"), f"v_gen4_{i}.mp4"),
        (os.path.join(working_anim_t, "animation-t.mp4"), f"t_gen4_{i}.mp4"),
    ]
    
    for src_path, dst_name in files:
        dst_path = os.path.join(anim_dir, dst_name)
        
        if os.path.exists(src_path):
            # 如果目标文件已存在，先删除（避免 move 报错）
            if os.path.exists(dst_path):
                os.remove(dst_path)
            try:
                shutil.move(src_path, dst_path)
                print(f"[{i}] 移动动画: {src_path} -> {dst_path}")
            except Exception as e:
                print(f"[{i}] 移动动画失败: {src_path}, error: {e}")
        else:
            print(f"[{i}] 未找到动画: {src_path}")

# --- 6. 核心处理逻辑 (单次执行) ---
i = args.time_location # 将参数赋值给循环变量 i

# 6.1 读取网格文件
import_file_name = os.path.join(msh_dir, f"model_gen4_{i}.msh.h5")
if not os.path.exists(import_file_name):
    print(f"错误：未找到网格文件 {import_file_name}")
    solver_session.exit()
    exit(1)

solver_session.tui.file.read_case(import_file_name)

# 6.2 执行 journal
solver_session.tui.file.read_journal(saved_journal_path)

# 6.3 启动仿真
solver_session.tui.solve.iterate(1000)

# 6.4 执行后处理 journal
solver_session.tui.file.read_journal(saved_post_journal_path)

time.sleep(2)
move_and_rename(i)

# 6.5 保存算例
case_file_name = f"model_gen4_{i}.cas.h5"
case_full_path = os.path.join(output_dir, case_file_name)
solver_session.tui.file.write_case_data(case_full_path)
print(f"[{i}] 算例已保存到 {case_full_path}")

# --- 7. 最后的清理工作 (日志 + 工作目录缓存) ---
# 定义需要清理的日志文件列表
files_to_delete = [
    "report-def-p-rfile.out",
    "report-def-v-rfile.out",
    "report-def-t-rfile.out",
]

# 遍历并删除日志文件
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

# 清空工作目录下的所有内容
working_dirs = [working_anim_t, working_anim_v]
for dir_path in working_dirs:
    if os.path.exists(dir_path):
        try:
            # 遍历文件夹下的所有文件和子文件夹并删除
            for filename in os.listdir(dir_path):
                file_path = os.path.join(dir_path, filename)
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path) # 删除文件或链接
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path) # 删除子文件夹
            print(f"[{i}] 已清空目录内容: {dir_path}")
        except Exception as e:
            print(f"[{i}] 清空目录失败 {dir_path}: {e}")
    else:
        print(f"[{i}] 目录不存在，无需清空: {dir_path}")

# --- 8. 退出 Fluent ---
solver_session.exit()
print(f"模型 {i} 的仿真计算完成！")