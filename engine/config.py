"""
===============================================================================
全局硬编码配置 (Hardcoded Configuration)
所有路径、SSH连接信息、环境变量等在此集中定义。
===============================================================================
"""
import os

# ============================================================================
# 本地 PC 路径配置
# ============================================================================
LOCAL_PATHS = {
    # SolidWorks 初始模型文件
    "sw_model": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.SLDPRT",
    # 外部 Excel 参数表（唯一数据源）
    "excel": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.xlsx",
    # SolidWorks 宏文件
    "sw_macro": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\Macro1.swp",
    # STEP 文件输出目录（SW 宏将 step 文件导出到此）
    "step_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\step",
    # SpaceClaim 可执行文件
    "sc_exe": r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
    # SpaceClaim 脚本文件
    "sc_script": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\spaceclaim_transit.scscript",
    # SCDOC 文件输出目录（SC 脚本将 scdoc 文件保存到此）
    "scdoc_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc",
    # 日志目录
    "log_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\logs",
}

# ============================================================================
# 远程工作站 (Windows 22H2) SSH 配置
# ============================================================================
REMOTE_CONFIG = {
    "host": "172.17.135.240",
    "port": 22,
    "username": "ps",
    "password": "abc@123",
    # 远程工程根目录
    "root_dir": r"D:\xkz_1020",
    # 远程 SCDOC 接收目录
    "scdoc_dir": r"D:\xkz_1020\scdoc",
    # Conda 环境名称
    "conda_env": "pyfluent",
    # 远程网格划分脚本
    "meshing_script": r"D:\xkz_1020\batch_meshing_gen4.py",
    # 远程求解脚本
    "solver_script": r"D:\xkz_1020\batch_solver_gen4.py",
    # 远程标志文件目录（用于轮询判断任务完成）
    "flag_dir": r"D:\xkz_1020\flags",
}

# ============================================================================
# 步骤名称枚举（与状态表和命令系统对应）
# ============================================================================
STEP_NAMES = ["SW", "SC", "Transfer", "Meshing", "Solver"]

# 步骤对应的中文显示名称
STEP_DISPLAY = {
    "SW": "SolidWorks导出",
    "SC": "SpaceClaim转换",
    "Transfer": "文件传输",
    "Meshing": "网格划分",
    "Solver": "仿真求解",
}

# 步骤顺序索引（用于判断"后续步骤"）
STEP_INDEX = {name: i for i, name in enumerate(STEP_NAMES)}

# ============================================================================
# 状态枚举
# ============================================================================
STATUS_WAITING = "Waiting"       # 等待中
STATUS_RUNNING = "Running"       # 运行中
STATUS_RETRYING = "Retrying"     # 重试中
STATUS_COMPLETED = "Completed"   # 已完成
STATUS_ERROR = "Error"           # 出错

ALL_STATUSES = [STATUS_WAITING, STATUS_RUNNING, STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR]

# ============================================================================
# 步骤对应的文件扩展名（用于 clean 命令）
# ============================================================================
STEP_FILE_PATTERNS = {
    "SW": "model_gen4_{config}.step",
    "SC": "model_gen4_{config}.scdoc",
    "Transfer": None,  # 传输不产生本地文件
    "Meshing": "model_gen4_{config}.msh.h5",
    "Solver": "model_gen4_{config}.cas.h5",  # cas 和 dat 都会清理
}

# ============================================================================
# IPC 通信配置
# ============================================================================
IPC_CONFIG = {
    # 使用本地 TCP socket 进行 IPC
    "host": "127.0.0.1",
    "port": 9527,
    # 共享状态数据库路径
    "db_path": os.path.join(LOCAL_PATHS["log_dir"], "pipeline_state.db"),
    # Socket 超时（秒）
    "timeout": 5.0,
}

# ============================================================================
# 调度引擎配置
# ============================================================================
ENGINE_CONFIG = {
    # 文件监控轮询间隔（秒）
    "watchdog_interval": 1.0,
    # SW 宏执行超时（秒）—— 导出所有构型的总时间
    "sw_macro_timeout": 3600,
    # SC 脚本执行超时（秒）
    "sc_timeout": 300,
    # 文件传输超时（秒）
    "transfer_timeout": 120,
    # 网格划分超时（秒）
    "meshing_timeout": 600,
    # 求解超时（秒）
    "solver_timeout": 7200,
    # 最大重试次数
    "max_retries": 3,
    # 全局状态刷新间隔（秒）
    "state_refresh_interval": 0.5,
}

# ============================================================================
# 确保必要目录存在
# ============================================================================
for key in ["step_dir", "scdoc_dir", "log_dir"]:
    path = LOCAL_PATHS.get(key, "")
    if path:
        os.makedirs(path, exist_ok=True)

# 远程标志目录（在首次 SSH 连接时创建）
REMOTE_FLAG_DIR = REMOTE_CONFIG["flag_dir"]
