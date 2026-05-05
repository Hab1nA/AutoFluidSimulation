# =============================================================================
# config.py — 集中配置管理
# 本文件包含所有硬编码路径、远程连接参数、参数组合定义等
# =============================================================================
import os
import sys
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# =============================================================================
# 本地配置 (Local PC)
# =============================================================================
LOCAL_CONFIG = {
    # SolidWorks 初始模型路径
    "sw_model_path": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.SLDPRT",
    # 外部 Excel 驱动文件
    "excel_path": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.xlsx",
    # SolidWorks VBA 宏文件（导出 STEP）
    "sw_macro_path": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\Macro1.swp",
    # STEP 文件保存目录
    "step_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\step",
    # SpaceClaim 可执行程序路径
    "spaceclaim_exe": r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
    # SpaceClaim 批处理脚本
    "sc_script": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\spaceclaim_transit.scscript",
    # SCDOC 文件保存目录
    "scdoc_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc",
    # 本地日志 / 数据库存放目录
    "log_dir": r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\logs",
    # SQLite 数据库文件名
    "db_filename": "pipeline_state.db",
}

# =============================================================================
# 远程工作站配置 (Remote Workstation - Windows 22H2)
# =============================================================================
REMOTE_CONFIG = {
    "host": "172.17.135.240",
    "port": 22,
    "username": "ps",
    "password": "abc@123",
    # 工程根目录
    "root_dir": r"D:\xkz_1020",
    # SCDOC 存放目录（远程）
    "scdoc_dir": r"D:\xkz_1020\scdoc",
    # Conda 环境名称
    "conda_env": "pyfluent",
    # 远程网格生成脚本
    "meshing_script": r"D:\xkz_1020\batch_meshing_gen4.py",
    # 远程 Fluent 求解脚本
    "solver_script": r"D:\xkz_1020\batch_solver_gen4.py",
    # 远程完成标志文件名模板 (config_id_done.txt)
    "done_flag_template": r"D:\xkz_1020\scdoc\{config_id}_done.txt",
    # SSH 连接超时（秒）
    "ssh_timeout": 15,
    # 远程任务轮询间隔（秒）
    "poll_interval": 15,
}

# =============================================================================
# 参数组合定义 (驱动 Excel 的变量取值范围)
# =============================================================================
@dataclass
class ConfigCombination:
    """单个构型的参数数据类"""
    id: str                                      # 构型唯一标识
    params: Dict[str, float] = field(default_factory=dict)  # Excel 单元格 -> 值


# 构型组合列表（每个 ConfigCombination 对应一组仿真参数）
# 用户根据需要修改此列表
CONFIG_COMBINATIONS: List[ConfigCombination] = [
    ConfigCombination(id="R2.5_L30_A15", params={"B2": 2.5, "B3": 30, "B4": 15}),
    ConfigCombination(id="R3.0_L35_A20", params={"B2": 3.0, "B3": 35, "B4": 20}),
    ConfigCombination(id="R3.5_L40_A25", params={"B2": 3.5, "B3": 40, "B4": 25}),
    # 在此添加更多构型...
]

# 默认参数（用于构建 Excel 列名映射等）
DEFAULT_PARAMS: Dict[str, float] = {}

# 向后兼容：保留 PARAMETER_SETS 供旧接口使用
PARAMETER_SETS = [
    {"config_id": c.id, "params": c.params}
    for c in CONFIG_COMBINATIONS
]

# =============================================================================
# 全局行为控制
# =============================================================================
GLOBAL_CONFIG = {
    # 最大重试次数（每个阶段的单任务失败后重试上限）
    "max_retry": 3,
    # SpaceClaim 进程超时（秒），超过则强制终止
    "sc_timeout": 300,
    # SolidWorks COM 操作重试间隔（秒）
    "sw_retry_interval": 5,
    # TUI 刷新频率（Hz）
    "tui_refresh_rate": 4,
    # 是否在启动时自动恢复上次 Computing 状态的任务检查
    "auto_recover": True,
}

# =============================================================================
# 工具函数
# =============================================================================
def ensure_directories():
    """自动创建必要的本地目录"""
    for _dir_key in ["step_dir", "scdoc_dir", "log_dir"]:
        os.makedirs(LOCAL_CONFIG[_dir_key], exist_ok=True)


def setup_logging():
    """配置全局日志（同时输出到控制台和文件）"""
    log_file = os.path.join(LOCAL_CONFIG["log_dir"], "pipeline.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    # 抑制 paramiko 的过多输出
    logging.getLogger("paramiko").setLevel(logging.WARNING)


# 启动时自动创建目录
ensure_directories()

# 构建完整的 SQLite 数据库路径
LOCAL_CONFIG["db_path"] = os.path.join(
    LOCAL_CONFIG["log_dir"], LOCAL_CONFIG["db_filename"]
)
