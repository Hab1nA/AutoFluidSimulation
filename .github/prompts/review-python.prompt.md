# Python 代码全面审查与修复

> **本 Prompt 仅审查 Python 代码。** Rust 代码请使用 `review-rust.prompt.md`，C# 代码请使用 `review-csharp.prompt.md`。

## 审查前准备

1. 阅读 `.github/instructions/python.instructions.md`，掌握 Python 代码规范
2. 阅读 `.github/instructions/implementation-planning.instructions.md` 中的架构概览，理解项目结构
3. 阅读 `README.md` 和 `docs/code-style-guide.md`，了解项目全貌
4. 使用仓库内 `.venv\Scripts\python.exe` 执行所有 Python 检查，不使用裸 `python`

## 🚫 语言隔离规则

- **只审查 `.py` 文件**，不要查看或引用 `.rs` 或 `.cs` 文件的内容
- 如果在审查过程中发现与 Rust/C# 的接口交互问题，记录为"跨语言接口问题"，但**不要修改**非 Python 文件
- 不要在建议中混入其他语言的代码片段或 API

## 审查范围

仅限以下 Python 文件：

- `engine/` — 调度引擎（daemon、scheduler、task_runner、state_manager、config 等）
- `executor/` — 任务执行器（remote_executor、sw_executor、spaceclaim_transit、cleaner）
- `ipc/` — IPC 服务端（protocol、server）
- `utils/` — 工具模块（ssh_client、logger、process_utils、excel_reader、tui_launcher）
- `tests/` — 测试文件
- `main.py`、`start_client.py`、`start_daemon.py` — 入口脚本
- `executor/remote_scripts/` — 远程执行脚本（Python 部分）

**重点审查**：`executor/remote_executor.py` 及相关远程执行代码，确保其逻辑与项目其他代码风格一致、衔接良好，不重复造轮子，充分利用已有的通用模块（如 `utils/ssh_client.py`、`utils/process_utils.py` 等）。

## 【审查流程】

### 第 1 步：全面系统性审查

分批次审查所有 Python 文件，重点评估：

- **代码质量**：可读性、可维护性、性能优化
- **编码规范性**：是否符合 `python.instructions.md` 中的命名规范、日志风格、类型注解要求
- **代码健壮性**：错误处理机制、边界条件处理、异常捕获
- **潜在功能性问题**：逻辑错误、竞态条件、资源泄漏、`subprocess.Popen` 不可序列化等
- **接口匹配度**：Python 模块间的接口是否一致、参数是否匹配
- **依赖关系合理性**：模块间依赖是否合理、是否复用了已有通用模块
- **远程执行重点**：`remote_executor.py` 的 SSH/SFTP 逻辑、超时处理、断线重连、与 `utils/ssh_client.py` 的复用关系

### 第 2 步：迭代修复循环

1. 初次审查后记录所有发现的问题
2. 针对问题进行代码修改
3. 修改完成后立即开展二次审查
4. 如二次审查仍发现问题，继续修改并再次审查
5. 如此循环迭代，直至连续两次审查均未发现任何代码问题

### 第 3 步：测试验证

```bash
.venv\Scripts\python.exe -m pytest tests/ -v
```

如测试失败，修复问题后重新测试，直到全部通过。

### 第 4 步：代码清理与优化

- 识别并移除所有冗余代码、已弃用功能模块及未使用的变量/函数
- 清理多余的注释内容
- 优化代码组织结构以提升逻辑清晰度和可扩展性
- 确保最终交付的代码达到高质量、高可维护性标准
