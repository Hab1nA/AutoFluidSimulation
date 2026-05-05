"""常量与枚举定义。"""

from enum import Enum
from typing import Dict, List


class StepName(str, Enum):
    """流水线阶段名称。"""

    SOLIDWORKS = "SW"
    SPACECLAIM = "SC"
    TRANSFER = "TRANSFER"
    MESHING = "MESHING"
    SOLVER = "SOLVER"


class StepStatus(str, Enum):
    """流水线状态枚举。"""

    WAITING = "Waiting"
    RUNNING = "Running"
    RETRYING = "Retrying"
    COMPLETED = "Completed"
    ERROR = "Error"


PIPELINE_STEPS: List[StepName] = [
    StepName.SOLIDWORKS,
    StepName.SPACECLAIM,
    StepName.TRANSFER,
    StepName.MESHING,
    StepName.SOLVER,
]

STEP_LABELS: Dict[StepName, str] = {
    StepName.SOLIDWORKS: "SW",
    StepName.SPACECLAIM: "SC",
    StepName.TRANSFER: "传输",
    StepName.MESHING: "网格划分",
    StepName.SOLVER: "仿真运行",
}

COMMAND_STEP_ALIASES: Dict[str, StepName] = {
    "sw": StepName.SOLIDWORKS,
    "solidworks": StepName.SOLIDWORKS,
    "sc": StepName.SPACECLAIM,
    "spaceclaim": StepName.SPACECLAIM,
    "transfer": StepName.TRANSFER,
    "mesh": StepName.MESHING,
    "meshing": StepName.MESHING,
    "solver": StepName.SOLVER,
}


def downstream_steps(start_step: StepName) -> List[StepName]:
    """获取从指定阶段开始的下游阶段列表。"""

    if start_step not in PIPELINE_STEPS:
        return []
    index = PIPELINE_STEPS.index(start_step)
    return PIPELINE_STEPS[index:]
