"""
===============================================================================
全局硬编码配置 (Hardcoded Configuration)
所有路径、SSH连接信息、环境变量等在此集中定义。
===============================================================================
"""
import os
import sys
import hashlib
from typing import Any, Dict, List, Optional, TypedDict, cast

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
    """读取环境变量覆盖值；若未设置、为空字符串或仅含空白则返回默认值。"""
    val = os.environ.get(key)
    if val is not None and val.strip():
        return val
    return default


# ============================================================================
# 配置字典类型定义 (TypedDict)
# ============================================================================


class LocalPathsConfig(TypedDict):
    sw_exe: str
    sw_model: str
    excel: str
    step_dir: str
    sc_exe: str
    sc_script: str
    sc_bridge: str
    scdoc_dir: str
    log_dir: str
    data_dir: str


class RemoteConfig(TypedDict):
    host: str
    port: int
    username: str
    password: str
    root_dir: str
    scdoc_dir: str
    msh_dir: str
    result_dir: str
    conda_env: str
    conda_exe: str
    meshing_script: str
    solver_script: str
    flag_dir: str


class IPCConfig(TypedDict):
    host: str
    port: int
    db_path: str
    timeout: float
    max_connections: int


class ProcessManagementConfig(TypedDict):
    min_valid_pid: int
    ipc_ready_timeout: int
    taskkill_timeout: int


class OperationTimeoutsConfig(TypedDict):
    sw_startup: int
    sw_dispatch_startup_delay: int
    sw_exit_wait_seconds: int
    sc_poll_interval: float
    ssh_connection: int
    dir_recursion_limit: int
    ssh_upload_max_retries: int


class EngineConfig(TypedDict):
    watchdog_interval: float
    sw_macro_timeout: int
    sw_close_doc_on_finish: bool
    sw_exit_on_finish: bool
    sw_visible: bool
    sw_max_retries: int
    sc_timeout: int
    transfer_timeout: int
    meshing_timeout: int
    solver_timeout: int
    max_retries: int
    state_refresh_interval: float
    sc_persistent_ready_timeout: int


# ============================================================================
# 本地 PC 路径配置
# ============================================================================
LOCAL_PATHS: LocalPathsConfig = {
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
    # SpaceClaim 脚本文件（Python 格式，兼容 V23 API）
    # 脚本位于项目 executor/ 目录下
    "sc_script": _env_override(
        "AUTOFLUID_SC_SCRIPT",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "executor", "spaceclaim_transit.py"),
    ),
    # C# 桥接程序（SpaceClaimBridge.exe）
    # 通过 Application.RunScript API 可靠调用 SpaceClaim 脚本
    "sc_bridge": _env_override(
        "AUTOFLUID_SC_BRIDGE",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge", "SpaceClaimBridge.exe"),
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
REMOTE_CONFIG: RemoteConfig = {
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
IPC_CONFIG: IPCConfig = {
    # 使用本地 TCP socket 进行 IPC
    "host": "127.0.0.1",
    "port": 9527,
    # 共享状态数据库路径
    "db_path": os.path.join(LOCAL_PATHS["data_dir"], "pipeline_state.db"),
    # Socket 超时（秒）
    "timeout": 5.0,
    # IPC 服务器最大同时连接数（防止资源耗尽）
    "max_connections": 10,
}

# ============================================================================
# 进程管理常数
# ============================================================================
PROCESS_MANAGEMENT: ProcessManagementConfig = {
    # 最小有效 PID（用于验证 PID 值）
    "min_valid_pid": 1,
    # IPC 就绪超时（秒） —— 启动 Daemon 后等待其 IPC 端口就绪的最长时间
    "ipc_ready_timeout": 20,
    # taskkill 命令超时（秒）
    "taskkill_timeout": 5,
}

# ============================================================================
# 操作超时常数
# ============================================================================
OPERATION_TIMEOUTS: OperationTimeoutsConfig = {
    # SolidWorks 启动超时（秒）
    "sw_startup": 60,
    # SW COM Dispatch 后等待窗口加载的延迟（秒）
    "sw_dispatch_startup_delay": 8,
    # SW ExitApp 后等待进程退出的最大秒数
    "sw_exit_wait_seconds": 15,
    # SC 进程轮询间隔（秒）
    "sc_poll_interval": 2.0,
    # SSH 连接超时（秒）
    "ssh_connection": 10,
    # 远程目录递归创建的深度限制
    "dir_recursion_limit": 32,
    # 文件上传重试的最大次数
    "ssh_upload_max_retries": 3,
}

# ============================================================================
# 配置指纹与数据库分片
# ============================================================================

def compute_config_fingerprint(configs: Dict[int, List[float]]) -> str:
    """
    计算构型组合的指纹（MD5 前 8 位）。

    同一组构型组合产生相同指纹，用于数据库文件分片——
    修改 Excel 设计表后构型组合变化，指纹随之变化，自动使用新数据库。
    """
    items = sorted(configs.items())
    canonical = ";".join(
        f"{name}:" + ",".join(f"{p:.6g}" for p in params)
        for name, params in items
    )
    return hashlib.md5(canonical.encode()).hexdigest()[:8]


def get_db_path_for_fingerprint(fingerprint: str) -> str:
    """根据配置指纹生成对应的数据库文件路径。"""
    return os.path.join(LOCAL_PATHS["data_dir"], f"pipeline_state_{fingerprint}.db")


# ============================================================================
# 调度引擎配置
# ============================================================================
ENGINE_CONFIG: EngineConfig = {
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
    # SC 常驻进程就绪超时（秒）—— 等待 SpaceClaim 启动和脚本初始化的最长时间
    "sc_persistent_ready_timeout": 180,
}


def get_step_filename(step_name: str, config_name: int) -> Optional[str]:
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


def _apply_env_overrides():
    """确保环境变量优先级高于 TOML 合并后的值。"""
    _env_local_keys = [
        ("sw_exe", "AUTOFLUID_SW_EXE"),
        ("sw_model", "AUTOFLUID_SW_MODEL"),
        ("excel", "AUTOFLUID_SW_EXCEL"),
        ("step_dir", "AUTOFLUID_STEP_DIR"),
        ("sc_exe", "AUTOFLUID_SC_EXE"),
        ("sc_script", "AUTOFLUID_SC_SCRIPT"),
        ("sc_bridge", "AUTOFLUID_SC_BRIDGE"),
        ("scdoc_dir", "AUTOFLUID_SCDOC_DIR"),
        ("log_dir", "AUTOFLUID_LOG_DIR"),
        ("data_dir", "AUTOFLUID_DATA_DIR"),
    ]
    for key, env_name in _env_local_keys:
        env_val = os.environ.get(env_name)
        if env_val:
            LOCAL_PATHS[key] = env_val

    _env_remote_keys = [
        ("host", "AUTOFLUID_SSH_HOST"),
        ("port", "AUTOFLUID_SSH_PORT"),
        ("username", "AUTOFLUID_SSH_USER"),
        ("password", "AUTOFLUID_SSH_PASSWORD"),
    ]
    for key, env_name in _env_remote_keys:
        env_val = os.environ.get(env_name)
        if env_val:
            if key == "port":
                REMOTE_CONFIG[key] = int(env_val)
            else:
                REMOTE_CONFIG[key] = env_val


def load_toml_config(toml_path: Optional[str] = None) -> dict[str, Any]:
    """
    从 autofluid_config.toml 加载配置。
    若文件不存在或无法解析，返回空字典。
    """
    if toml_path is None:
        toml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "autofluid_config.toml"
        )

    if not os.path.exists(toml_path):
        return {}

    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(toml_path, "rb") as f:
                return cast(dict[str, Any], tomllib.load(f))
        else:
            import toml
            return cast(dict[str, Any], toml.load(toml_path))
    except Exception:
        return {}


def reload_config_from_toml() -> bool:
    """重新加载 TOML 配置文件并合并到全局配置。环境变量保持最高优先级。"""
    toml_data = load_toml_config()
    if toml_data:
        if "local_paths" in toml_data:
            LOCAL_PATHS.update(toml_data["local_paths"])
        if "remote_config" in toml_data:
            REMOTE_CONFIG.update(toml_data["remote_config"])
        if "step_file_patterns" in toml_data:
            STEP_FILE_PATTERNS.update(toml_data["step_file_patterns"])
        if "engine_config" in toml_data:
            ENGINE_CONFIG.update(toml_data["engine_config"])
        if "operation_timeouts" in toml_data:
            OPERATION_TIMEOUTS.update(toml_data["operation_timeouts"])
        _apply_env_overrides()
        return True
    return False


# 启动时尝试加载 TOML 配置，合并到默认值中
reload_config_from_toml()


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

