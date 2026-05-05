"""数据模型定义。"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ConfigRow:
    """单个构型的数据行。"""

    name: str
    param1: Optional[float]
    param2: Optional[float]
    param3: Optional[float]
    param4: Optional[float]

    def params(self) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """以元组形式返回参数。"""

        return (self.param1, self.param2, self.param3, self.param4)
