"""
===============================================================================
IPC 通信协议 (Inter-Process Communication Protocol)
基于 JSON 的 TCP Socket 协议，用于 TUI 客户端与后台 Daemon 之间的通信。

协议格式（每条消息以换行符 \n 分隔）：
{
    "command": "命令名",
    "params": { ... },
    "request_id": "唯一请求ID"
}

响应格式：
{
    "status": "ok" | "error",
    "data": { ... },
    "message": "描述信息",
    "request_id": "与请求相同的ID"
}
===============================================================================
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from utils.logger import setup_logger

logger = setup_logger(__name__)


# ============================================================================
# 命令常量定义
#
# ⚠️ 同步要求：这些常量必须与 Rust 侧 `autofluid-tui/src/ipc/protocol.rs` 中的
# 命令常量保持完全一致。添加新命令时，请同时更新两处定义。
# 可通过运行 `python -m pytest tests/test_ipc_protocol.py -v -k "command"` 验证。
# ============================================================================

# ---- 引擎控制命令 ----
CMD_START = "start"             # 启动/继续流水线
CMD_PAUSE = "pause"             # 暂停流水线
CMD_STOP = "stop"               # 停止引擎（full_quit）
CMD_CHECK = "check"             # 系统自检

# ---- 状态操作命令 ----
CMD_RESET_STEP = "reset_step"   # 重置指定构型指定步骤

# ---- 清理命令 ----
CMD_CLEAN_STEP = "clean_step"   # 清理指定步骤文件

# ---- 查询命令 ----
CMD_GET_ALL_STATUS = "get_all_status"   # 获取所有构型状态
CMD_GET_STATISTICS = "get_statistics"   # 获取统计信息
CMD_GET_ENGINE_STATUS = "get_engine_status"  # 获取引擎状态
CMD_GET_LOG_ENTRIES = "get_log_entries"  # 增量拉取日志条目
CMD_GET_DASHBOARD = "get_dashboard"      # 批量获取状态、引擎信息和日志增量
CMD_RELOAD_CONFIG = "reload_config"      # 重新加载 TOML 配置文件

# ---- LocalWorker 命令 ----
CMD_WORKER_REGISTER = "worker_register"    # LocalWorker 注册
CMD_WORKER_HEARTBEAT = "worker_heartbeat"  # LocalWorker 心跳
CMD_WORKER_POLL = "worker_poll"            # LocalWorker 拉取待执行任务
CMD_WORKER_STEP_COMPLETE = "worker_step_complete"  # LocalWorker 上报任务完成
CMD_WORKER_STEP_ERROR = "worker_step_error"        # LocalWorker 上报任务失败

# ---- Worker 生命周期管理命令 ----
CMD_WORKER_START = "worker_start"          # 启动所有 worker（本地 + 工作站）
CMD_WORKER_STOP = "worker_stop"            # 停止所有 worker 并关闭 SSH 隧道
CMD_WORKER_RESTART = "worker_restart"      # 重启所有 worker


# ============================================================================
# 消息构造与解析
# ============================================================================

def create_request(
    command: str,
    params: dict[str, Any] | None = None,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """
    创建一个标准请求消息。

    Args:
        command: 命令名
        params: 参数字典

    Returns:
        请求消息字典
    """
    request = {
        "command": command,
        "params": params or {},
        "request_id": str(uuid.uuid4())[:8],
    }
    if auth_token:
        request["auth_token"] = auth_token
    return request


def create_response(status: str, request_id: str, data: Any = None,
                    message: str = "") -> dict[str, Any]:
    """
    创建一个标准响应消息。

    Args:
        status: "ok" 或 "error"
        request_id: 对应的请求 ID
        data: 响应数据
        message: 描述信息

    Returns:
        响应消息字典
    """
    return {
        "status": status,
        "data": data,
        "message": message,
        "request_id": request_id,
    }


def serialize(msg: dict[str, Any]) -> bytes:
    """将消息字典序列化为 JSON 字节串（末尾加换行符）。"""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def deserialize(data: bytes) -> dict[str, Any] | None:
    """
    从字节串反序列化为消息字典。

    Args:
        data: 原始字节数据

    Returns:
        消息字典，解析失败返回 None
    """
    try:
        text = data.decode("utf-8").strip()
        if not text:
            return None
        obj = json.loads(text)
        if not isinstance(obj, dict):
            logger.warning(f"消息反序列化后不是对象: {type(obj).__name__}")
            return None
        return obj
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"消息反序列化失败: {e} (原始数据前100字节: {data[:100]!r})")
        return None
