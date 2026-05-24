"""
配置指纹与数据库分片。

根据 Excel 构型组合计算唯一指纹，用于数据库文件分片——
相同构型组合复用同一数据库，修改设计表后自动使用新数据库。
"""

from __future__ import annotations

import hashlib
import os


def compute_config_fingerprint(configs: dict[int, list[float]]) -> str:
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
    from engine.config import LOCAL_PATHS
    return os.path.join(LOCAL_PATHS["data_dir"], f"pipeline_state_{fingerprint}.db")
