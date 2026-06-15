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
# 模块级 TOML 配置加载
# ============================================================================

def _load_toml_at_startup() -> dict[str, Any]:
    """在模块加载时尝试加载 autofluid_config.toml。
    若文件不存在或无法解析，返回空字典。
    """
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


# 模块加载时读取 TOML 配置（用于后续初始化）
_TOML_CONFIG: dict[str, Any] = _load_toml_at_startup()


# ============================================================================
# 环境变量覆盖（用于部署/迁移）
# ============================================================================

def _env_override(key: str, default: str) -> str:
    """读取环境变量覆盖值；若未设置、为空字符串或仅含空白则返回默认值。"""
    val = os.environ.get(key)
    if val is not None and val.strip():
        return val
    return default


def _toml_or_default(toml_section: str, key: str, default: Any) -> Any:
    """从 TOML 配置中获取值，若不存在则返回默认值。
    用于在模块初始化时优先使用 TOML 中的配置。
    """
    section = _TOML_CONFIG.get(toml_section)
    if isinstance(section, dict) and key in section:
        return section[key]
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


class WorkstationConfig(RemoteConfig, total=False):
    id: str
    reachable_host: str
    reachable_port: int
    connectivity_mode: str
    postprocess_script: str
    postprocess_output_dir: str
    notes: str


class IPCConfig(TypedDict):
    host: str
    port: int
    db_path: str
    timeout: float
    max_connections: int
    auth_token: str


class ProcessManagementConfig(TypedDict):
    min_valid_pid: int
    ipc_ready_timeout: int
    taskkill_timeout: int


class OperationTimeoutsConfig(TypedDict):
    sw_startup: int
    sw_dispatch_startup_delay: int
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
        _toml_or_default("local_paths", "sw_exe", r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"),
    ),
    # SolidWorks 初始模型文件
    "sw_model": _env_override(
        "AUTOFLUID_SW_MODEL",
        _toml_or_default("local_paths", "sw_model", r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.SLDPRT"),
    ),
    # 外部 Excel 参数表（唯一数据源）
    "excel": _env_override(
        "AUTOFLUID_SW_EXCEL",
        _toml_or_default("local_paths", "excel", r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.xlsx"),
    ),
    # STEP 文件输出目录（直接 COM 调用导出 STEP 到此）
    "step_dir": _env_override(
        "AUTOFLUID_STEP_DIR",
        _toml_or_default("local_paths", "step_dir", r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\step"),
    ),
    # SpaceClaim 可执行文件
    "sc_exe": _env_override(
        "AUTOFLUID_SC_EXE",
        _toml_or_default("local_paths", "sc_exe", r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe"),
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
        _toml_or_default("local_paths", "scdoc_dir", r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc"),
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
    "host": os.environ.get("AUTOFLUID_SSH_HOST", _toml_or_default("remote_config", "host", "172.17.135.240")),
    "port": int(os.environ.get("AUTOFLUID_SSH_PORT", _toml_or_default("remote_config", "port", "22"))),
    "username": os.environ.get("AUTOFLUID_SSH_USER", _toml_or_default("remote_config", "username", "ps")),
    "password": os.environ.get("AUTOFLUID_SSH_PASSWORD", _toml_or_default("remote_config", "password", "")),
    # 仿真工作目录
    "working_dir": _toml_or_default("remote_config", "working_dir", r"D:\xkz_1020\workingdir"),
    # 远程脚本部署目录（.jou/.set/.wft/.py 上传目标）
    "scripts_dir": _toml_or_default("remote_config", "scripts_dir", r"D:\xkz_1020"),
    # 仿真引用文件目录（pdf/fla/chemkin 文件）
    "ref_files_dir": _toml_or_default("remote_config", "ref_files_dir", r"D:\xkz_1020\fluent_chemkin_files"),
    # 远程 SCDOC 接收目录
    "scdoc_dir": _toml_or_default("remote_config", "scdoc_dir", r"D:\xkz_1020\scdoc"),
    # 远程网格划分输出目录 (.msh.h5)
    "msh_dir": _toml_or_default("remote_config", "msh_dir", r"D:\xkz_1020\msh"),
    # 远程仿真求解输出目录 (.cas.h5, .dat.h5)
    "result_dir": _toml_or_default("remote_config", "result_dir", r"D:\xkz_1020\case"),
    # 仿真标志目录（用于轮询判断任务完成）
    "flag_dir": _toml_or_default("remote_config", "flag_dir", r"D:\xkz_1020\flags"),
    # Conda 环境名称
    "conda_env": _toml_or_default("remote_config", "conda_env", "pyfluent"),
    # Conda 可执行文件完整路径（SSH 非交互会话中 PATH 不含 conda，需用完整路径）
    "conda_exe": _toml_or_default("remote_config", "conda_exe", r"C:\ProgramData\anaconda3\Scripts\conda.exe"),
    # 远程 ANSYS 安装根目录
    "mpi_bin_dir": os.environ.get("AUTOFLUID_REMOTE_MPI_BIN_DIR", _toml_or_default("remote_config", "mpi_bin_dir", r"C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin")),
}

DEFAULT_WORKSTATION_ID = "default"


def _workstation_from_remote_config(
    workstation_id: str = DEFAULT_WORKSTATION_ID,
) -> WorkstationConfig:
    """Build a workstation entry from the legacy single-workstation config."""
    workstation = cast(WorkstationConfig, dict(REMOTE_CONFIG))
    workstation["id"] = workstation_id
    return workstation


WORKSTATIONS: list[WorkstationConfig] = [_workstation_from_remote_config()]


def _sync_default_workstation_reachable_env() -> None:
    """Apply server-reachable env overrides to the legacy default workstation."""
    if not WORKSTATIONS:
        return
    default_workstation = WORKSTATIONS[0]
    for key, env_name in [
        ("reachable_host", "AUTOFLUID_SSH_REACHABLE_HOST"),
        ("reachable_port", "AUTOFLUID_SSH_REACHABLE_PORT"),
        ("connectivity_mode", "AUTOFLUID_SSH_CONNECTIVITY_MODE"),
    ]:
        env_val = os.environ.get(env_name)
        if not env_val:
            continue
        if key == "reachable_host":
            default_workstation["reachable_host"] = env_val
        elif key == "reachable_port":
            default_workstation["reachable_port"] = int(env_val)
        else:
            default_workstation["connectivity_mode"] = env_val


_sync_default_workstation_reachable_env()


def get_workstation_config(
    workstation_id: str = DEFAULT_WORKSTATION_ID,
) -> WorkstationConfig:
    """Return a copy of one configured remote workstation."""
    for workstation in WORKSTATIONS:
        if workstation.get("id") == workstation_id:
            return _effective_workstation_config(workstation)
    raise KeyError(f"未知工作站配置: {workstation_id}")


def is_server_mode() -> bool:
    """Return True when daemon/TUI are running in remote-server mode."""
    return os.environ.get("AUTOFLUID_SERVER_MODE", "").lower() == "server"


def _effective_workstation_config(workstation: WorkstationConfig) -> WorkstationConfig:
    """Return workstation config with server-reachable host applied."""
    result = cast(WorkstationConfig, dict(workstation))
    reachable_host = str(result.get("reachable_host", "")).strip()
    if is_server_mode() and reachable_host:
        result["host"] = reachable_host
        if "reachable_port" in result:
            result["port"] = int(result["reachable_port"])
    return result

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
    # 远程部署时启用；为空表示本地兼容模式不校验
    "auth_token": os.environ.get("AUTOFLUID_IPC_AUTH_TOKEN", ""),
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
    "sw_startup": _toml_or_default("solidworks", "sw_startup", 60),
    # SW COM Dispatch 后等待窗口加载的延迟（秒）
    "sw_dispatch_startup_delay": _toml_or_default("solidworks", "sw_dispatch_startup_delay", 8),
    # SC 进程轮询间隔（秒）
    "sc_poll_interval": _toml_or_default("spaceclaim", "sc_poll_interval", 2.0),
    # SSH 连接超时（秒）
    "ssh_connection": _toml_or_default("global_settings", "ssh_connection", 10),
    # 远程目录递归创建的深度限制
    "dir_recursion_limit": _toml_or_default("global_settings", "dir_recursion_limit", 32),
    # 文件上传重试的最大次数
    "ssh_upload_max_retries": _toml_or_default("global_settings", "ssh_upload_max_retries", 3),
    # SpaceClaim 启动相关超时
    # 启动 exe 后等待进程在系统中出现的最大秒数
    "sc_process_appear_timeout": _toml_or_default("spaceclaim", "sc_process_appear_timeout", 120),
    # 进程出现后等待主窗口可交互的最大秒数
    "sc_gui_ready_timeout": _toml_or_default("spaceclaim", "sc_gui_ready_timeout", 30),
    # 主窗口就绪后额外等待后台加载稳定的秒数
    "sc_gui_stable_delay": _toml_or_default("spaceclaim", "sc_gui_stable_delay", 15),
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
    "watchdog_interval": _toml_or_default("global_settings", "watchdog_interval", 1.0),
    # SW 宏执行超时（秒）—— 导出所有构型的总时间
    "sw_macro_timeout": _toml_or_default("solidworks", "sw_macro_timeout", 3600),
    # SW 自动化行为控制
    # - sw_close_doc_on_finish: 宏完成后关闭已打开的模型文档（减少资源占用）
    # - sw_visible: 是否显示 SolidWorks 主窗口
    "sw_close_doc_on_finish": _toml_or_default("solidworks", "sw_close_doc_on_finish", True),
    "sw_visible": _toml_or_default("solidworks", "sw_visible", True),
    # SC 脚本执行超时（秒）
    "sc_timeout": _toml_or_default("spaceclaim", "sc_timeout", 300),
    # 文件传输超时（秒）
    "transfer_timeout": _toml_or_default("global_settings", "transfer_timeout", 120),
    # 网格划分超时（秒）
    "meshing_timeout": _toml_or_default("meshing", "meshing_timeout", 600),
    # Fluent Meshing 并行核心数。高核心数在体网格拓扑准备阶段可能更慢或不稳定。
    "meshing_processor_count": _toml_or_default("meshing", "meshing_processor_count", 8),
    # 求解超时（秒）
    "solver_timeout": _toml_or_default("solver", "solver_timeout", 7200),
    # Fluent Solver 并行核心数。求解阶段通常可使用更多核心。
    "solver_processor_count": _toml_or_default("solver", "solver_processor_count", 128),
    # Fluent Solver 每构型迭代次数。传递给 batch_solver_gen4.py --iterate-count。
    "solver_iteration_count": _toml_or_default("solver", "solver_iteration_count", 1000),
    # 最大重试次数
    "max_retries": _toml_or_default("global_settings", "max_retries", 3),
    # 全局状态刷新间隔（秒）
    "state_refresh_interval": _toml_or_default("global_settings", "state_refresh_interval", 0.5),
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

def ensure_directories() -> None:
    """创建必要的本地目录。"""
    directory_keys = ["scdoc_dir", "log_dir", "data_dir"] if is_server_mode() else [
        "step_dir", "scdoc_dir", "log_dir", "data_dir",
    ]
    for key in directory_keys:
        path_value = LOCAL_PATHS.get(key, "")
        if not path_value:
            continue
        path = str(path_value)
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
        ("reachable_host", "AUTOFLUID_SSH_REACHABLE_HOST"),
        ("reachable_port", "AUTOFLUID_SSH_REACHABLE_PORT"),
        ("connectivity_mode", "AUTOFLUID_SSH_CONNECTIVITY_MODE"),
        ("username", "AUTOFLUID_SSH_USER"),
        ("password", "AUTOFLUID_SSH_PASSWORD"),
    ]
    remote_env_overrides: dict[str, str | int] = {}
    for key, env_name in _env_remote_keys:
        env_val = os.environ.get(env_name)
        if env_val:
            if key in {"port", "reachable_port"}:
                REMOTE_CONFIG[key] = int(env_val)
                remote_env_overrides[key] = int(env_val)
            else:
                REMOTE_CONFIG[key] = env_val
                remote_env_overrides[key] = env_val
    if remote_env_overrides:
        _sync_default_workstation_reachable_env()

    _env_ipc_keys = [
        ("host", "AUTOFLUID_IPC_HOST"),
        ("port", "AUTOFLUID_IPC_PORT"),
        ("auth_token", "AUTOFLUID_IPC_AUTH_TOKEN"),
    ]
    for key, env_name in _env_ipc_keys:
        env_val = os.environ.get(env_name)
        if env_val:
            if key == "port":
                IPC_CONFIG[key] = int(env_val)
            else:
                IPC_CONFIG[key] = env_val

    solver_iteration_count = os.environ.get("AUTOFLUID_SOLVER_ITERATION_COUNT")
    if solver_iteration_count:
        ENGINE_CONFIG["solver_iteration_count"] = int(solver_iteration_count)


def _sync_default_workstation() -> None:
    """Keep WORKSTATIONS[0] aligned with REMOTE_CONFIG in legacy mode."""
    default = _workstation_from_remote_config()
    if not WORKSTATIONS:
        WORKSTATIONS.append(default)
        return
    if WORKSTATIONS[0].get("id") == DEFAULT_WORKSTATION_ID:
        WORKSTATIONS[0] = default


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


def _expand_config_value(value: Any) -> Any:
    """Recursively expand ${VAR} references in TOML-derived config values."""
    if isinstance(value, dict):
        return {k: _expand_config_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_config_value(v) for v in value]
    return _expand_env_vars(value)


def _expand_dict_env_vars(d: dict) -> dict:
    """展开字典中所有字符串值的 ${VAR} 环境变量引用。"""
    return cast(dict, _expand_config_value(d))


def _normalize_workstation_config(raw: dict[str, Any], index: int) -> WorkstationConfig:
    """Merge a TOML workstation entry with legacy defaults."""
    merged: dict[str, Any] = dict(REMOTE_CONFIG)
    merged.update(raw)
    merged["id"] = str(merged.get("id") or f"WS-{index + 1}")
    for port_key in ("port", "reachable_port"):
        if port_key in merged:
            merged[port_key] = int(merged[port_key])
    return cast(WorkstationConfig, merged)


def reload_config_from_toml() -> bool:
    """重新加载 TOML 配置文件并合并到全局配置。环境变量保持最高优先级。

    TOML 中支持 ${VAR} 语法引用环境变量（如 ``password = "${AUTOFLUID_SSH_PASSWORD}"``）。
    """
    toml_data = load_toml_config()
    if toml_data:
        # 展开 ${VAR} 环境变量引用（如 password = "${AUTOFLUID_SSH_PASSWORD}"）
        toml_data = cast(dict[str, Any], _expand_config_value(toml_data))
        if "local_paths" in toml_data:
            LOCAL_PATHS.update(toml_data["local_paths"])
        if "remote_config" in toml_data:
            REMOTE_CONFIG.update(toml_data["remote_config"])
        if "ipc_config" in toml_data:
            ipc_updates = {
                key: value
                for key, value in toml_data["ipc_config"].items()
                if key in IPC_CONFIG
            }
            if "port" in ipc_updates:
                ipc_updates["port"] = int(ipc_updates["port"])
            IPC_CONFIG.update(cast(IPCConfig, ipc_updates))
        explicit_workstations = "workstations" in toml_data
        if explicit_workstations:
            workstation_items = toml_data["workstations"]
            if isinstance(workstation_items, list):
                WORKSTATIONS[:] = [
                    _normalize_workstation_config(item, idx)
                    for idx, item in enumerate(workstation_items)
                    if isinstance(item, dict)
                ]
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
        if not explicit_workstations:
            _sync_default_workstation()
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

    if is_server_mode():
        for workstation in WORKSTATIONS:
            ws_id = str(workstation.get("id", DEFAULT_WORKSTATION_ID))
            raw_host = str(workstation.get("host", "")).strip()
            raw_port = int(workstation.get("port", 22) or 22)
            reachable_host = str(workstation.get("reachable_host", "")).strip()
            if not reachable_host:
                warnings.append(
                    f"工作站 {ws_id} 在 server 模式下缺少 "
                    f"AUTOFLUID_SSH_REACHABLE_HOST，将检查原始地址 "
                    f"{raw_host}:{raw_port}"
                )
                continue
            if "reachable_port" not in workstation:
                warnings.append(
                    f"工作站 {ws_id} 在 server 模式下缺少 "
                    f"AUTOFLUID_SSH_REACHABLE_PORT，将使用原始端口 {raw_port}"
                )
        return warnings

    if not os.path.exists(LOCAL_PATHS["sw_model"]):
        warnings.append(f"SW 模型文件不存在: {LOCAL_PATHS['sw_model']}")

    if not os.path.exists(LOCAL_PATHS["excel"]):
        warnings.append(f"Excel 参数表不存在: {LOCAL_PATHS['excel']}")

    if not os.path.exists(LOCAL_PATHS["sc_exe"]):
        warnings.append(f"SpaceClaim 可执行文件不存在: {LOCAL_PATHS['sc_exe']}")

    if not os.path.exists(LOCAL_PATHS["sw_exe"]):
        warnings.append(f"SolidWorks 可执行文件不存在: {LOCAL_PATHS['sw_exe']} —— 将仅通过 COM 方式启动")

    return warnings

