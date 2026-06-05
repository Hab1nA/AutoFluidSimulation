# 详细日志广播层聚合输出功能解析报告

> 本报告说明详细日志栏目中逐构型日志治理功能的设计目标、实现路径、当前行为和维护边界。

---

## 1. 背景

AutoFluid 在一次流水线运行中可能处理上百个构型。许多执行路径会自然地产生逐构型日志，例如：

- Excel 读取每个构型参数
- 调度器更新单个构型状态
- SpaceClaim 池分配某个构型到某个槽位
- 重置或恢复单个构型步骤

这些日志对文件日志有调试价值，但如果原样广播到 TUI 的详细日志栏目，会在启动、恢复、重试或批量调度时瞬间刷出大量相似条目，淹没真正需要人注意的事件。因此详细日志栏需要一层“面向 UI 的降噪策略”，而不是要求每个业务模块自行决定是否输出。

---

## 2. 设计目标

该功能最初的目标是将逐构型日志合并为一条摘要，例如：

```text
[聚合] 10 条逐构型日志已合并: [Excel] 读取构型{n}: 参数 = [...]
```

后续根据实际运行观察，摘要本身也会成为详细日志栏里的噪声。当前实现已经调整为：

- 逐构型 `INFO` / `DEBUG` 日志不进入详细日志栏目。
- 广播层不再生成 `[聚合] ...` 摘要条目。
- 文件日志仍保留原始逐构型日志，便于事后排查。
- `WARNING`、`ERROR`、`CRITICAL` 级别的构型日志始终放行，避免吞掉故障信号。

因此，现在的“聚合输出功能”准确地说是详细日志广播层的逐构型日志聚合识别与静默过滤功能。

---

## 3. 实现位置

核心实现位于 `utils/logger.py`：

- `LogEntry`：详细日志 IPC 传输使用的结构化日志条目。
- `LogBroadcastHandler`：安装在 root logger 上的广播 handler，负责维护 TUI 可查询的环形缓冲区。
- `POLLING_COMMANDS`：高频 IPC 轮询命令过滤列表。
- `_CONFIG_SCOPED_LOG_PATTERNS`：逐构型日志识别规则。
- `_is_config_scoped_log()`：逐构型日志过滤判定。
- `_is_polling_log()`：IPC 轮询日志过滤判定。

相关补充位于 `ipc/server.py`：

- 未完成握手的端口探测日志通过 `extra={"broadcast": False}` 标记为不广播。

回归测试位于 `tests/test_detail_log.py`：

- `test_broadcast_false_record_is_suppressed`
- `test_config_scoped_logs_are_suppressed`
- `test_config_scoped_log_suppression_matches_config_equals`
- `test_config_scoped_log_suppression_matches_operation_context`
- `test_single_config_scoped_log_is_suppressed`
- `test_config_scoped_excel_logs_do_not_emit_aggregate_summary`
- `test_config_scoped_log_passes_through_warning_and_above`

---

## 4. 广播处理流程

`LogBroadcastHandler.emit()` 是进入详细日志栏的总入口。当前处理顺序如下：

1. 如果日志记录带有 `extra={"broadcast": False}`，直接丢弃，不进入详细日志缓冲区。
2. 如果日志是 IPC 高频轮询日志，直接丢弃。
3. 如果日志是低级别逐构型日志，直接丢弃。
4. 其他日志转换为 `LogEntry`，写入线程安全环形缓冲区。

伪代码如下：

```python
def emit(record):
    if record.broadcast is False:
        return
    if is_polling_log(record):
        return
    if is_config_scoped_log(record):
        return
    buffer.append(entry_from_record(record))
```

这个顺序很重要：

- `broadcast=False` 是显式控制，应最高优先级。
- IPC 轮询日志数量极高，应尽早过滤。
- 逐构型过滤发生在结构化条目创建之前，避免分配 ID、占用缓冲区、参与统计。

---

## 5. 逐构型识别规则

当前逐构型识别只看消息文本，命中以下任意模式即认为是逐构型日志：

```text
构型\s*\d+
config 或 config_name 后接 = 或 : 再接数字
```

典型会被过滤的低级别日志：

```text
[Excel] 读取构型 9: 参数 = [7.0, 16.0, 14.0, 23.0]
状态更新: 构型1 [sc] -> Completed
已重置 config=3 step=all
[SC-Pool] 构型5 命令已发送 (槽位3, run=5)
```

不会因为包含构型信息而被过滤的高优先级日志：

```text
WARNING 检测到重复入队：构型8 Transfer 已在队列中
ERROR 构型3 Meshing 异常重试 3 次后放弃
CRITICAL 构型5 致命异常: RuntimeError: 连接中断
```

这条边界是有意保留的：详细日志栏可以少显示过程噪声，但不能少显示异常风险。

---

## 6. 与文件日志的关系

该功能只影响 TUI 详细日志广播层，不改变普通文件日志写入。

也就是说，业务模块仍然可以继续使用常规 logger 输出逐构型调试信息。文件日志中仍可看到完整过程，详细日志栏则只展示适合实时阅读的事件。

这种分层让业务模块不必各自维护“是否应该给 TUI 看”的判断，避免相同规则分散到 `excel_reader`、调度器、执行器等不同位置。

---

## 7. `broadcast=False` 的用途

`broadcast=False` 是显式的单条日志广播开关，适合处理以下情况：

- 文件日志有价值，但 TUI 显示价值很低。
- 日志来自健康检查、端口探测或内部握手细节。
- 该日志不是逐构型日志，但仍不适合出现在详细日志栏目。

当前使用案例是 IPC 未完成握手日志：

```python
logger.debug(
    f"[IPC] IPC 客户端断开 (未完成握手): {addr}",
    extra={"broadcast": False},
)
```

这类日志通常由端口探测或短连接造成。保留文件日志可以帮助定位连接行为，但广播到 TUI 容易让用户误以为出现了第二个客户端会话或连接错误。

---

## 8. 当前行为示例

### 8.1 Excel 逐构型读取

文件日志：

```text
[DEBUG] [utils.excel_reader] [Excel] 读取构型 0: 参数 = [...]
[DEBUG] [utils.excel_reader] [Excel] 读取构型 1: 参数 = [...]
...
[INFO] [utils.excel_reader] [Excel] 成功读取 10 个构型配置
```

详细日志栏：

```text
[utils.excel_reader] [Excel] 成功读取 10 个构型配置
```

不会显示：

```text
[utils.excel_reader] [聚合] 10 条逐构型日志已合并: ...
```

### 8.2 调度器状态更新

文件日志可以保留：

```text
状态更新: 构型1 [sc] -> Completed
状态更新: 构型2 [sc] -> Completed
```

详细日志栏不显示这些低级别逐构型状态细节。

### 8.3 构型异常

文件日志和详细日志栏都会显示：

```text
构型3 Meshing 异常重试 3 次后放弃
```

前提是日志级别为 `ERROR` 或更高。

---

## 9. 测试覆盖

当前测试覆盖以下行为：

- `broadcast=False` 日志不进入详细日志。
- IPC 高频轮询命令不进入详细日志。
- 中文 `构型N` 形式的低级别日志不进入详细日志。
- `config=N` 形式的低级别日志不进入详细日志。
- 带槽位、队列、run id 等上下文的逐构型日志不进入详细日志。
- 单条逐构型日志也不会进入详细日志。
- Excel 逐构型日志不会生成 `[聚合] ...` 摘要条目。
- `WARNING` 及以上级别的逐构型日志会被放行。

这些测试重点保证两个边界：

- 不让批量过程噪声污染 TUI。
- 不让异常信号被降噪逻辑误杀。

---

## 10. 维护建议

新增日志时建议遵循以下规则：

- 普通进度或状态变化日志可以继续写入业务模块，无需手动判断 TUI 展示。
- 如果日志描述单个构型且属于 `INFO` 或 `DEBUG`，广播层会自动过滤。
- 如果单个构型出现异常，应使用 `WARNING` 或更高级别，这样会进入详细日志。
- 如果某条非构型日志也不适合广播，使用 `extra={"broadcast": False}`。
- 不建议重新引入 `[聚合] ...` 摘要显示，除非 UI 明确增加单独的统计区域。

如果未来需要展示批量统计，更合适的方式是新增专门的状态字段或统计面板，而不是把统计摘要塞回详细日志文本流。

---

## 11. 已知边界

当前逐构型识别依赖文本模式，因此有以下边界：

- 如果日志使用其他构型标识格式，例如 `case 001`、`cfg-001`，当前不会自动过滤。
- 如果低级别日志虽然包含 `构型N`，但确实希望出现在详细日志栏，需要改为更高日志级别或调整过滤规则。
- 过滤只影响广播缓冲区，不影响文件日志大小。

这些边界是当前实现有意保持的保守取舍：先覆盖项目内最常见的构型命名格式，避免过宽匹配误伤普通日志。

