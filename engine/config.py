"""
===============================================================================
全局硬编码配置 (Hardcoded Configuration)
所有路径、SSH连接信息、环境变量等在此集中定义。
===============================================================================
"""
import os
import sys

# 加载 .env 文件中的环境变量（需 python-dotenv）
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装时静默跳过，依赖系统环境变量

# ============================================================================
# 环境变量覆盖（用于部署/迁移）
# ============================================================================

def _env_override(key: str, default: str) -> str:
    """读取环境变量覆盖值；若未设置或为空则返回默认值。"""
    val = os.environ.get(key)
    return val if val else default


# ============================================================================
# 本地 PC 路径配置
# ============================================================================
LOCAL_PATHS = {
    # SolidWorks 可执行文件路径（备选启动方案：COM Dispatch 失败时直接启动）
    "sw_exe": _env_override(
        "AUTOFLUID_SW_EXE",
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    ),
    # SolidWorks 初始模型文件
    "sw_model": _env_override(
        "AUTOFLUID_SW_MODEL",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.SLDPRT",
    ),
    # 外部 Excel 参数表（唯一数据源）
    "excel": _env_override(
        "AUTOFLUID_SW_EXCEL",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.xlsx",
    ),
    # STEP 文件输出目录（直接 COM 调用导出 STEP 到此）
    "step_dir": _env_override(
        "AUTOFLUID_STEP_DIR",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\step",
    ),
    # SpaceClaim 可执行文件
    "sc_exe": _env_override(
        "AUTOFLUID_SC_EXE",
        r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
    ),
    # SpaceClaim 脚本文件
    "sc_script": _env_override(
        "AUTOFLUID_SC_SCRIPT",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\spaceclaim_transit.scscript",
    ),
    # SCDOC 文件输出目录（SC 脚本将 scdoc 文件保存到此）
    "scdoc_dir": _env_override(
        "AUTOFLUID_SCDOC_DIR",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc",
    ),
    # 日志目录
    "log_dir": _env_override(
        "AUTOFLUID_LOG_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
    ),
    # 数据库/数据目录（独立于日志目录）
    "data_dir": _env_override(
        "AUTOFLUID_DATA_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    ),
}

# ============================================================================
# 远程工作站 (Windows 22H2) SSH 配置
# ============================================================================
REMOTE_CONFIG = {
    "host": os.environ.get("AUTOFLUID_SSH_HOST", "172.17.135.240"),
    "port": int(os.environ.get("AUTOFLUID_SSH_PORT", "22")),
    "username": os.environ.get("AUTOFLUID_SSH_USER", "ps"),
    "password": os.environ.get("AUTOFLUID_SSH_PASSWORD", ""),
    # 远程工程根目录
    "root_dir": r"D:\xkz_1020",
    # 远程 SCDOC 接收目录
    "scdoc_dir": r"D:\xkz_1020\scdoc",
    # 远程网格划分输出目录 (.msh.h5)
    "msh_dir": r"D:\xkz_1020\msh",
    # 远程仿真求解输出目录 (.cas.h5, .dat.h5)
    "result_dir": r"D:\xkz_1020\case",
    # Conda 环境名称
    "conda_env": "pyfluent",
    # Conda 可执行文件完整路径（SSH 非交互会话中 PATH 不含 conda，需用完整路径）
    "conda_exe": r"C:\ProgramData\anaconda3\Scripts\conda.exe",
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
STATUS_WAITING   = "Waiting"       # 等待中
STATUS_RUNNING   = "Running"       # 运行中
STATUS_PAUSED    = "Paused"        # 已暂停（用户手动暂停）
STATUS_RETRYING  = "Retrying"      # 重试中
STATUS_COMPLETED = "Completed"     # 已完成
STATUS_ERROR     = "Error"         # 出错

ALL_STATUSES = [STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR]

# ============================================================================
# 步骤对应的文件扩展名（用于 clean 命令）
# ============================================================================
STEP_FILE_PATTERNS = {
    "SW": "model_gen4.SLDPRT_{config}.step",
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
    "db_path": os.path.join(LOCAL_PATHS["data_dir"], "pipeline_state.db"),
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
    # SW 自动化行为控制
    # - sw_close_doc_on_finish: 宏完成后关闭已打开的模型文档（减少资源占用）
    # - sw_exit_on_finish: 宏完成后退出 SolidWorks（默认为 True，确保程序运行整洁性）
    # - sw_visible: 是否显示 SolidWorks 主窗口
    "sw_close_doc_on_finish": True,
    "sw_exit_on_finish": True,
    "sw_visible": True,
    # SW 宏执行最大重试次数（默认 1 = 不重试，SW 启动/执行开销大）
    "sw_max_retries": 2,
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


def get_step_filename(step_name: str, config_name: int):
    """根据 STEP_FILE_PATTERNS 生成文件名。"""
    pattern = STEP_FILE_PATTERNS.get(step_name)
    if not pattern:
        return None
    try:
        return pattern.format(config=config_name)
    except (KeyError, ValueError):
        return None

# ============================================================================
# 确保必要目录存在 & 配置验证（由 daemon 启动时调用）
# ============================================================================

def ensure_directories():
    """创建必要的本地目录。"""
    for key in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]:
        path = LOCAL_PATHS.get(key, "")
        if path:
            try:
                os.makedirs(path, exist_ok=True)
            except PermissionError as e:
                print(f"[WARNING] 权限不足，无法创建目录: {path}: {e}", file=sys.stderr)
            except OSError as e:
                print(f"[WARNING] 无法创建目录 {path}: {e}", file=sys.stderr)


def validate_config() -> list:
    """验证配置完整性，返回警告信息列表。"""
    warnings = []

    if not REMOTE_CONFIG["password"]:
        warnings.append(
            "SSH 密码未设置！请设置环境变量 AUTOFLUID_SSH_PASSWORD，"
            "或在 config.py 中配置 password 字段"
        )

    if not os.path.exists(LOCAL_PATHS["sw_model"]):
        warnings.append(f"SW 模型文件不存在: {LOCAL_PATHS['sw_model']}")

    if not os.path.exists(LOCAL_PATHS["excel"]):
        warnings.append(f"Excel 参数表不存在: {LOCAL_PATHS['excel']}")

    if not os.path.exists(LOCAL_PATHS["sc_exe"]):
        warnings.append(f"SpaceClaim 可执行文件不存在: {LOCAL_PATHS['sc_exe']}")

    if not os.path.exists(LOCAL_PATHS["sw_exe"]):
        warnings.append(f"SolidWorks 可执行文件不存在: {LOCAL_PATHS['sw_exe']} —— 将仅通过 COM 方式启动")

    return warnings



