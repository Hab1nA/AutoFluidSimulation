"""共享 pytest fixture。

提供：
- tmp_db_path: 隔离的临时数据库路径，自动 patch IPC_CONFIG["db_path"]
- tmp_db: 返回一个已初始化的 StateManager，使用隔离的临时数据库

所有使用 StateManager 的测试应通过这两个 fixture 获取 DB，
避免直接修改模块级全局变量 IPC_CONFIG["db_path"]，
从而支持 pytest-xdist 并行执行。
"""
from __future__ import annotations

import pytest

from engine.config import IPC_CONFIG
from engine.state_manager import StateManager


@pytest.fixture(scope="function")
def tmp_db_path(tmp_path, monkeypatch):
    """提供隔离的 SQLite 数据库路径，用 monkeypatch 安全设置 IPC_CONFIG["db_path"]。

    作用域为 function（每个测试独立），确保 pytest-xdist 并行安全。
    monkeypatch.setitem 是线程安全的，fixture 结束后自动恢复。
    tmp_path 由 pytest 自动创建和清理（测试结束后删除临时目录）。
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setitem(IPC_CONFIG, "db_path", db_path)
    return db_path


@pytest.fixture(scope="function")
def tmp_db(tmp_db_path):
    """提供已初始化的 StateManager，使用隔离的临时数据库。

    依赖 tmp_db_path → tmp_path + monkeypatch，因此仅 `tmp_db_path` 执行
    IPC_CONFIG patch（无重复 patching 风险）。作用域为 function。

    等同于:
        state = StateManager(db_path=tmp_db_path)
    """
    return StateManager(db_path=tmp_db_path)
