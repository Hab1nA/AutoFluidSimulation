"""硬编码配置定义。"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalPaths:
    """本地路径配置。"""

    solidworks_model: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\model_gen4.SLDPRT"
    )
    excel_path: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\model_gen4.xlsx"
    )
    macro_path: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\Macro1.swp"
    )
    step_dir: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\step"
    )
    spaceclaim_exe: str = r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe"
    sc_script: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\spaceclaim_transit.scscript"
    )
    scdoc_dir: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\scdoc"
    )
    log_dir: str = (
        r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)"
        r"\solidworks_models\logs"
    )


@dataclass(frozen=True)
class RemoteConfig:
    """远程工作站配置。"""

    host: str = "172.17.135.240"
    port: int = 22
    username: str = "ps"
    password: str = "abc@123"
    project_root: str = r"D:\xkz_1020"
    remote_scdoc_dir: str = r"D:\xkz_1020\scdoc"
    conda_env: str = "pyfluent"
    conda_activate_cmd: str = "call activate pyfluent"
    meshing_script: str = r"D:\xkz_1020\batch_meshing_gen4.py"
    solver_script: str = r"D:\xkz_1020\batch_solver_gen4.py"
    meshing_clean_cmd: str = ""
    solver_clean_cmd: str = ""


@dataclass
class PipelineConfig:
    """流水线总控配置。"""

    local: LocalPaths = field(default_factory=LocalPaths)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    ipc_host: str = "127.0.0.1"
    ipc_port: int = 9234
    poll_interval: float = 2.0
    sc_workers: int = 2
    transfer_workers: int = 2
    meshing_workers: int = 2
    solver_workers: int = 2
    max_retries: int = 2
    db_path: str = ""

    def __post_init__(self) -> None:
        if not self.db_path:
            self.db_path = str(Path(self.local.log_dir) / "pipeline_state.db")


CONFIG = PipelineConfig()
