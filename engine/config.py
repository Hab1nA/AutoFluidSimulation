"""
===============================================================================
全局硬编码配置 (Hardcoded Configuration)
所有路径、SSH连接信息、环境变量等在此集中定义。
===============================================================================
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, TypedDict, cast

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
    remote_scripts_dir: str


class RemoteConfig(TypedDict):
    host: str
    port: int
    username: str
    password: str
    working_dir: str
    scripts_dir: str
    ref_files_dir: str
    scdoc_dir: str
    msh_dir: str
    result_dir: str
    flag_dir: str
    conda_env: str
    conda_exe: str
    mpi_bin_dir: str


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
    # SpaceClaim 启动相关超时
    sc_process_appear_timeout: int  # 启动exe后等待进程出现(秒)
    sc_gui_ready_timeout: int       # 进程出现后等待主窗口可交互(秒)
    sc_gui_stable_delay: int        # 主窗口就绪后额外等待后台稳定(秒)


class EngineConfig(TypedDict):
    watchdog_interval: float
    sw_macro_timeout: int
    sw_close_doc_on_finish: bool
    sw_exit_on_finish: bool
    sw_visible: bool
    sc_timeout: int
    transfer_timeout: int
    meshing_timeout: int
    meshing_processor_count: int
    solver_timeout: int
    solver_processor_count: int
    solver_iteration_count: int
    max_retries: int
    state_refresh_interval: float
    sc_persistent_ready_timeout: int
    sc_scdoc_stable_seconds: float


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
    # 脚本位于项目 executor/ 目录下（固定相对于项目根目录）
    "sc_script": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "executor", "spaceclaim_transit.py",
    ),
    # C# 桥接程序（SpaceClaimBridge.exe）
    # 通过 Application.RunScript API 可靠调用 SpaceClaim 脚本
    # 固定位于项目 bridge/ 目录下
    "sc_bridge": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bridge", "SpaceClaimBridge.exe",
    ),
    # SCDOC 文件输出目录（SC 脚本将 scdoc 文件保存到此）
    "scdoc_dir": _env_override(
        "AUTOFLUID_SCDOC_DIR",
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc",
    ),
    # 日志目录（固定位于项目 logs/ 目录下）
    "log_dir": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs",
    ),
    # 数据库/数据目录（固定位于项目 data/ 目录下）
    "data_dir": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
    ),
    # 远程脚本本地目录（固定位于项目 executor/remote_scripts/ 目录下）
    "remote_scripts_dir": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "executor", "remote_scripts",
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
    # 仿真工作目录
    "working_dir": r"D:\xkz_1020\workingdir",
    # 远程脚本部署目录（.jou/.set/.wft/.py 上传目标）
    "scripts_dir": r"D:\xkz_1020",
    # 仿真引用文件目录（pdf/fla/chemkin 文件）
    "ref_files_dir": r"D:\xkz_1020\fluent_chemkin_files",
    # 远程 SCDOC 接收目录
    "scdoc_dir": r"D:\xkz_1020\scdoc",
    # 远程网格划分输出目录 (.msh.h5)
    "msh_dir": r"D:\xkz_1020\msh",
    # 远程仿真求解输出目录 (.cas.h5, .dat.h5)
    "result_dir": r"D:\xkz_1020\case",
    # 仿真标志目录（用于轮询判断任务完成）
    "flag_dir": r"D:\xkz_1020\flags",
    # Conda 环境名称
    "conda_env": "pyfluent",
    # Conda 可执行文件完整路径（SSH 非交互会话中 PATH 不含 conda，需用完整路径）
    "conda_exe": r"C:\ProgramData\anaconda3\Scripts\conda.exe",
    # 远程 ANSYS 安装根目录
    "mpi_bin_dir": os.environ.get("AUTOFLUID_REMOTE_MPI_BIN_DIR", r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin"),
}

# ============================================================================
# 步骤名称枚举（与状态表和命令系统对应）
# ============================================================================
STEP_NAMES = ["sw", "sc", "transfer", "meshing", "solver"]

# 步骤对应的中文显示名称
STEP_DISPLAY = {
    "sw": "SolidWorks导出",
    "sc": "SpaceClaim转换",
    "transfer": "文件传输",
    "meshing": "网格划分",
    "solver": "仿真求解",
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
    "sw": "model_gen4.SLDPRT_{config}.step",
    "sc": "model_gen4_{config}.scdoc",
    "transfer": None,  # 传输不产生本地文件
    "meshing": "model_gen4_{config}.msh.h5",
    "solver": "model_gen4_{config}.cas.h5",
    "solverdata": "model_gen4_{config}.dat.h5",
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
    # SpaceClaim 启动相关超时
    # 启动 exe 后等待进程在系统中出现的最大秒数
    "sc_process_appear_timeout": 120,
    # 进程出现后等待主窗口可交互的最大秒数
    "sc_gui_ready_timeout": 30,
    # 主窗口就绪后额外等待后台加载稳定的秒数
    "sc_gui_stable_delay": 15,
}

# ============================================================================
# 配置指纹与数据库分片（实际实现已迁入 config_fingerprint.py）
# ============================================================================
from engine.config_fingerprint import compute_config_fingerprint, get_db_path_for_fingerprint  # noqa: F401


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
    # SC 脚本执行超时（秒）
    "sc_timeout": 300,
    # 文件传输超时（秒）
    "transfer_timeout": 120,
    # 网格划分超时（秒）
    "meshing_timeout": 600,
    # Fluent Meshing 并行核心数。高核心数在体网格拓扑准备阶段可能更慢或不稳定。
    "meshing_processor_count": 8,
    # 求解超时（秒）
    "solver_timeout": 7200,
    # Fluent Solver 并行核心数。求解阶段通常可使用更多核心。
    "solver_processor_count": 128,
    # Fluent Solver 每构型迭代次数。传递给 batch_solver_gen4.py --iterate-count。
    "solver_iteration_count": 1000,
    # 最大重试次数
    "max_retries": 3,
    # 全局状态刷新间隔（秒）
    "state_refresh_interval": 0.5,
    # SC 常驻进程就绪超时（秒）—— 等待 SpaceClaim 启动和脚本初始化的最长时间
    "sc_persistent_ready_timeout": 180,
    # SC SCDOC 文件大小稳定判定窗口（秒）—— SaveAs 完成的判定依据
    "sc_scdoc_stable_seconds": 3.0,
}


def get_step_filename(step_name: str, config_name: int) -> str | None:
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


def load_toml_config(toml_path: str | None = None) -> dict[str, Any]:
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


def _expand_env_vars(value: Any) -> Any:
    """展开字符串值中的 ${VAR} 环境变量引用。非字符串值原样返回。"""
    if not isinstance(value, str):
        return value
    def _replace(m: re.Match[str]) -> str:
        var_name = m.group(1)
        return os.environ.get(var_name, m.group(0))  # 未定义则保留原文
    return re.sub(r'\$\{(\w+)\}', _replace, value)


def _expand_dict_env_vars(d: dict) -> dict:
    """展开字典中所有字符串值的 ${VAR} 环境变量引用。"""
    return {k: _expand_env_vars(v) for k, v in d.items()}


def reload_config_from_toml() -> bool:
    """重新加载 TOML 配置文件并合并到全局配置。环境变量保持最高优先级。

    TOML 中支持 ${VAR} 语法引用环境变量（如 ``password = "${AUTOFLUID_SSH_PASSWORD}"``）。
    """
    toml_data = load_toml_config()
    if toml_data:
        # 展开 ${VAR} 环境变量引用（如 password = "${AUTOFLUID_SSH_PASSWORD}"）
        for section_key in toml_data:
            if isinstance(toml_data[section_key], dict):
                toml_data[section_key] = _expand_dict_env_vars(toml_data[section_key])
        if "local_paths" in toml_data:
            LOCAL_PATHS.update(toml_data["local_paths"])
        if "remote_config" in toml_data:
            REMOTE_CONFIG.update(toml_data["remote_config"])
        if "step_file_patterns" in toml_data:
            STEP_FILE_PATTERNS.update(toml_data["step_file_patterns"])
        # 新分类格式：按工具维度拆分为 solidworks / spaceclaim / global_settings
        if "solidworks" in toml_data:
            ENGINE_CONFIG.update(
                cast(EngineConfig, {k: v for k, v in toml_data["solidworks"].items()
                 if k in ENGINE_CONFIG})
            )
            OPERATION_TIMEOUTS.update(
                cast(OperationTimeoutsConfig, {k: v for k, v in toml_data["solidworks"].items()
                 if k in OPERATION_TIMEOUTS})
            )
        if "spaceclaim" in toml_data:
            ENGINE_CONFIG.update(
                cast(EngineConfig, {k: v for k, v in toml_data["spaceclaim"].items()
                 if k in ENGINE_CONFIG})
            )
            OPERATION_TIMEOUTS.update(
                cast(OperationTimeoutsConfig, {k: v for k, v in toml_data["spaceclaim"].items()
                 if k in OPERATION_TIMEOUTS})
            )
        if "meshing" in toml_data:
            ENGINE_CONFIG.update(
                cast(EngineConfig, {k: v for k, v in toml_data["meshing"].items()
                 if k in ENGINE_CONFIG})
            )
        if "solver" in toml_data:
            ENGINE_CONFIG.update(
                cast(EngineConfig, {k: v for k, v in toml_data["solver"].items()
                 if k in ENGINE_CONFIG})
            )
        if "global_settings" in toml_data:
            ENGINE_CONFIG.update(
                cast(EngineConfig, {k: v for k, v in toml_data["global_settings"].items()
                 if k in ENGINE_CONFIG})
            )
            OPERATION_TIMEOUTS.update(
                cast(OperationTimeoutsConfig, {k: v for k, v in toml_data["global_settings"].items()
                 if k in OPERATION_TIMEOUTS})
            )
        # 向后兼容：旧格式 engine_config / operation_timeouts
        if "engine_config" in toml_data:
            ENGINE_CONFIG.update(toml_data["engine_config"])
        if "operation_timeouts" in toml_data:
            OPERATION_TIMEOUTS.update(toml_data["operation_timeouts"])
        _apply_env_overrides()
        return True
    return False


# 启动时尝试加载 TOML 配置，合并到默认值中
# 注：由 daemon 启动时通过 ensure_directories() + validate_config() 显式调用，
# 避免模块加载时自动执行带来的测试副作用
# reload_config_from_toml()


def validate_config() -> list[str]:
    """验证配置完整性，返回警告信息列表。"""
    warnings: list[str] = []

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

