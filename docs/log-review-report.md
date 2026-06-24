# 项目日志系统审查报告

> 审查日期：2026-06-24  
> 审查范围：AutoFluidSimulation 全项目 Python / Rust 日志  
> 审查维度：日志级别合理性、内容一致性、排版格式

---

## 问题概述

- **现象**：项目日志系统存在级别设定不当、内容不一致、前缀缺失、排版格式不统一等问题。
- **影响范围**：所有 Python 模块（`engine/`、`executor/`、`ipc/`、`utils/`）、Rust TUI（`autofluid-tui/`）
- **问题域**：运行时日志

---

## 调查过程

### 已检查的资料

| 文件 | 发现 |
|------|------|
| `AGENTS.md` | 确认项目架构（Python daemon + Rust TUI + C# bridge） |
| `.github/instructions/python.instructions.md` | 日志规范：`[模块/子模块]` 英文标签前缀 |
| `.claude/skills/autofluid-coding/SKILL.md` | 编码总入口，引向各语言技能 |
| `utils/logger.py` | `PrefixStrippingFormatter` 自动去除消息前缀；`setup_logger` 使用 `%(name)s` 字段 |
| `engine/daemon.py` | 200+ 条 log 调用，含 `[Worker]`、`[LocalWorker]`、`[CONFIG]`、`[ServerMode]` 等标签 |
| `engine/scheduler/main.py` | 大量 log，部分缺 `[Scheduler]` 前缀 |
| `engine/scheduler/utils.py` | 使用非标准 logger 变量名 `_pg_logger` |
| `executor/remote_executor.py` | 标签使用基本规范，少量级别问题 |
| `executor/spaceclaim_transit.py` | 独立的 `_SpaceClaimLogger` 类，`.format()` 风格 |
| `engine/config.py` | 目录创建失败用 `warning` 级别 |
| `autofluid-tui/src/state/log_buffer.rs` | `log_info_message()` 始终加 "高级信息:" 前缀 |
| `engine/local_worker.py` | 部分 `warning` 缺乏上下文 |

### 关键发现

- 项目有一个**双重前缀机制**：源代码中手写 `[Tag]` 前缀 → `PrefixStrippingFormatter` 自动剥离 → 最终日志用 `%(name)s` 标识模块。这意味着手写前缀仅作源码可读性用途。
- 日志规范文档定义了约 12 个标准标签（`[SW]`、`[SC]`、`[Scheduler]`、`[IPC]` 等），但实际代码中出现了未文档化的标签如 `[CONFIG]`、`[ServerMode]`、`[LocalWorker]`、`[SC-Pool]`、`[AlertWatcher]`、`[Cleaner]`、`[MeshingMonitor]`、`[BarrierMonitor]`、`[Sync]`、`[Resume]`、`[Excel]`。

---

## 根因分析

### 假设 1: 日志级别选用不精确（置信度：高）

**证据链**：

| 位置 | 当前级别 | 建议级别 | 理由 |
|------|---------|---------|------|
| `engine/config.py:584-586` | `warning` | `error` | 目录创建失败是**致命错误**，后续文件写入将全部失败。`warning` 容易被忽略。 |
| `engine/daemon.py:588-594` | `warning` | `info` | 自动检测到状态不一致并执行恢复是**正常的自愈逻辑**，不是需要关注的异常。 |
| `engine/daemon.py:1161` | `warning` | `debug` | "刷新配置后断开旧 SSH 连接异常" 是配置热更新时的常规清理，失败不影响主流程。 |
| `executor/remote_executor.py:528-530` | `warning` | `error` | 同步状态持久化失败可能导致**数据丢失**，应为 `error`。 |

**代码引用**：

`engine/config.py:584-586`：
```python
except PermissionError as e:
    logger.warning("权限不足，无法创建目录: %s: %s", path, e)
except OSError as e:
    logger.warning("无法创建目录 %s: %s", path, e)
```

`engine/daemon.py:588-594`：
```python
logger.warning("检测到引擎状态为 running 但调度器暂停标志已置位，执行恢复")
# ...
logger.warning("检测到引擎状态为 running 但调度器线程已退出，重新启动流水线")
```

### 假设 2: 日志标签使用不一致（置信度：高）

**证据链**：

1. **同一概念多标签**：`[Worker]` vs `[LocalWorker]` 混用：
   - `daemon.py:379` — `logger.info("[Worker] shutdown watchdog 清理结果: %s", ...)`
   - `daemon.py:421` — `logger.warning("[LocalWorker] 强制终止异常: %s", e)`
   - `daemon.py:747` — `logger.debug("[LocalWorker] 非 Windows 环境跳过本机 LocalWorker 自动唤起")`

2. **调度器核心控制路径缺 `[Scheduler]` 标签**：
   - `scheduler/main.py:499` — `logger.info("收到暂停指令")`
   - `scheduler/main.py:505` — `logger.info("流水线已暂停，正在运行的步骤将继续执行直到完成")`
   - `scheduler/main.py:981` — `logger.info("收到继续指令")`
   - `scheduler/main.py:1038` — `logger.info("流水线已恢复运行")`
   - `scheduler/main.py:1042` — `logger.info("收到停止指令")`
   - `scheduler/main.py:1092` — `logger.info("流水线已停止")`

3. **Daemon 核心路径缺标签**：
   - `daemon.py:149` — `logger.info(f"PID 文件中的进程已退出 (PID: {stale_pid})，清理残留 PID 文件")`
   - `daemon.py:155` — `logger.info(f"进程锁已获取 (PID: {current_pid})")`
   - `daemon.py:165` — `logger.info(f"进程锁已释放 (PID: {current_pid})")`
   - `daemon.py:343` — `logger.info("收到 Ctrl+C，守护进程正在退出...")`

4. **未文档化的标签**：`[CONFIG]`、`[ServerMode]`、`[SC-Pool]`、`[AlertWatcher]`、`[Cleaner]`、`[MeshingMonitor]`、`[BarrierMonitor]`、`[Sync]` 等未列入 `.github/instructions/python.instructions.md` 的标签表。

### 假设 3: Rust TUI 日志前缀误导用户（置信度：高）

**证据链**：

`autofluid-tui/src/state/log_buffer.rs:243-245`：
```rust
fn log_info_message(message: &str) {
    match info_message_level(message) {
        log::Level::Error => log::error!("高级信息: {message}"),
        log::Level::Warn => log::warn!("高级信息: {message}"),
        _ => log::info!("高级信息: {message}"),
    }
}
```

- 当 daemon 发来错误信息时，TUI 日志输出为 `[ERROR] 高级信息: ❌ 错误描述`，语义矛盾：错误 ≠ 高级信息。
- `info_message_level` 函数基于内容关键词（`失败`、`错误`、`警告`）反推级别是脆弱的启发式方法，可能误判。

### 假设 4: 日志格式化风格不统一（置信度：中）

**证据链**：

1. **f-string vs `%s` 混用**：主体使用 f-string，但以下位置用 `%s`：
   - `engine/daemon.py:379` — `"[Worker] shutdown watchdog 清理结果: %s"`
   - `engine/config.py:584` — `"权限不足，无法创建目录: %s: %s"`
   - `engine/state_manager.py:92,98` — `"数据库回滚异常: %s"`

2. **`exc_info=True` 冗余**：`file_monitor.py:276` 在 f-string 中嵌入了异常类型和消息，同时传了 `exc_info=True`，导致信息在消息体和 traceback 中重复出现：
   ```python
   logger.error(f"文件扫描异常 ({type(e).__name__}: {e})", exc_info=True)
   ```

3. **`scheduler/utils.py`** 使用 `_pg_logger` 而非约定俗成的 `logger`，降低代码可读性。

### 假设 5: `PrefixStrippingFormatter` 设计存在语义歧义（置信度：低）

- `_strip_manual_message_prefix` 的正则 `^\[(?=[^\]\r\n]*[A-Za-z\u4e00-\u9fff])[^\]\r\n]{1,40}\]\s*` 会剥离所有含字母的 `[tag]`。
- 如果未来有消息确实需要以 `[SomeText]` 开头（比如错误码），会被错误剥离。目前未发现实际案例，但设计上缺乏"保留"机制。

---

## 修复方案

### 方案 A: 日志级别批量修正 ⭐⭐⭐ 推荐

- **描述**：将上述 5 处级别使用不当的代码进行修正（`warning`→`error` 或 `warning`→`info`/`debug`）。
- **涉及文件**：
  - `engine/config.py` — `ensure_directories()` 中两处 `warning` 改为 `error`
  - `engine/daemon.py` — 状态自愈日志 `warning` 改为 `info`（3 处）
  - `executor/remote_executor.py` — 同步状态保存失败 `warning` 改为 `error`
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：低。仅改变日志可见性，不影响程序逻辑。
- **需要同步修改的测试**：无

### 方案 B: 补全缺失的模块前缀标签 ⭐⭐⭐ 推荐

- **描述**：为调度器控制路径和 daemon 核心路径的日志补上 `[Scheduler]`/`[Daemon]` 前缀，统一 `[Worker]` vs `[LocalWorker]` 为一套标签体系。
- **涉及文件**：
  - `engine/scheduler/main.py` — ~10 处补 `[Scheduler]` 前缀
  - `engine/daemon.py` — ~8 处补 `[Daemon]` 前缀
- **改动量**：20-100 行
- **复杂度**：简单
- **副作用风险**：低。`PrefixStrippingFormatter` 自动剥离前缀，不影响最终输出。
- **需要同步修改的测试**：无

### 方案 C: 修复 Rust TUI "高级信息" 前缀 ⭐⭐ 可行

- **描述**：移除 `log_info_message()` 中的 "高级信息:" 前缀，改为直接输出 message，或使用更中性的描述（如 `[Daemon] {message}`）。
- **涉及文件**：
  - `autofluid-tui/src/state/log_buffer.rs` — `log_info_message()` 函数
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：低。TUI 日志面板中所有 daemon 转发日志会丢失 "高级信息" 标记，但消息级别已由 log level 区分。
- **需要同步修改的测试**：无

### 方案 D: 更新日志规范文档补充新标签 ⭐⭐ 可行

- **描述**：将 `[CONFIG]`、`[SC-Pool]`、`[Cleaner]`、`[Sync]`、`[MeshingMonitor]`、`[BarrierMonitor]` 等已实际使用的标签补充到 `.github/instructions/python.instructions.md` 日志规范表中。
- **涉及文件**：
  - `.github/instructions/python.instructions.md`
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：无
- **需要同步修改的测试**：无

### 方案 E: 统一 `%s` 为 f-string ⭐ 可行（低优先级）

- **描述**：将少数几处 `%s` 风格的日志格式化为 f-string，确保全项目风格一致。注意：`logging` 的 `%s` 是惰性求值的（仅在日志级别启用时才格式化），改为 f-string 会失去此优化，但实际影响可忽略。
- **涉及文件**：
  - `engine/daemon.py`、`engine/config.py`、`engine/state_manager.py`
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：极低。f-string 在日志被过滤时仍会求值，但性能差异微乎其微。
- **需要同步修改的测试**：无

---

## 建议下一步

1. **优先执行**：方案 A（级别修正）+ 方案 B（补前缀），共约 30-40 行改动，影响面小、收益明确。
2. **其次**：方案 C（Rust TUI 前缀修复），消除用户可见的语义矛盾。
3. **文档同步**：方案 D（更新规范表），确保代码与文档一致。
4. **低优先级**：方案 E（风格统一），可在后续日常维护中逐步完成，不阻塞发布。
5. **后续验证**：运行 `ruff check .` 确保无格式回归；手动检查 daemon 启动/暂停/恢复/停止日志输出，确认标签完整、级别合理。
