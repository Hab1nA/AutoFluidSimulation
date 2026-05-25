# Rust 代码全面审查与修复

> **本 Prompt 仅审查 Rust 代码。** Python 代码请使用 `review-python.prompt.md`，C# 代码请使用 `review-csharp.prompt.md`。

## 审查前准备

1. 阅读 `.github/instructions/rust.instructions.md`，掌握 Rust 代码规范
2. 阅读 `.github/instructions/implementation-planning.instructions.md` 中的架构概览，理解项目结构
3. 阅读 `README.md` 和 `docs/code-style-guide.md`（Rust 命名规范部分），了解项目全貌

## 🚫 语言隔离规则

- **只审查 `autofluid-tui/` 下的 `.rs` 和 `.toml` 文件**，不要查看或引用 `.py` 或 `.cs` 文件的内容
- 如果在审查过程中发现与 Python/C# 的接口交互问题，记录为"跨语言接口问题"，但**不要修改**非 Rust 文件
- 不要在建议中混入其他语言的代码片段或 API

## 审查范围

仅限 `autofluid-tui/` 目录下的所有 Rust 文件：

- `src/main.rs` — 入口：tokio runtime + App 初始化
- `src/daemon_mgr.rs` — Daemon 进程管理
- `src/event_handler.rs` + `src/event_handler/` — 事件分发与处理
- `src/ipc.rs` + `src/ipc/` — IPC 客户端
- `src/state.rs` + `src/state/` — 应用状态管理
- `src/settings/` — 设置页面逻辑
- `src/ui.rs` + `src/ui/` — UI 渲染组件
- `src/text_buffer.rs` — 文本缓冲区
- `src/theme.rs` — 主题/颜色定义
- `src/utils.rs` — 工具函数
- `Cargo.toml` — 依赖配置

## 【审查流程】

### 第 1 步：全面系统性审查

分批次审查所有 Rust 文件，重点评估：

- **代码质量**：可读性、可维护性、性能优化、是否符合 Rust 惯用法（idiomatic Rust）
- **编码规范性**：是否符合 `rust.instructions.md` 中的命名规范、文档注释要求
- **代码健壮性**：`Result`/`Option` 使用是否正确、是否过度使用 `unwrap()`、panic 风险
- **异步编程**：tokio runtime 使用是否正确、是否在异步上下文中阻塞（如 `std::thread::sleep`）、资源泄漏
- **接口匹配度**：IPC 协议与 Python 端的 JSON 格式是否一致、配置字段名是否与 `autofluid_config.toml` 匹配
- **依赖关系合理性**：`Cargo.toml` 依赖是否合理、是否有未使用的依赖
- **UI 与状态管理**：ratatui 渲染逻辑、状态更新的响应性、事件处理的完整性

### 第 2 步：迭代修复循环

1. 初次审查后记录所有发现的问题
2. 针对问题进行代码修改
3. 修改完成后立即开展二次审查
4. 如二次审查仍发现问题，继续修改并再次审查
5. 如此循环迭代，直至连续两次审查均未发现任何代码问题

### 第 3 步：测试验证

```bash
cd autofluid-tui
cargo check
cargo clippy -- -D warnings
cargo test
```

如测试失败，修复问题后重新测试，直到全部通过。

### 第 4 步：代码清理与优化

- 识别并移除所有冗余代码、未使用的变量/函数/`use` 导入
- 清理多余的注释内容
- 优化代码组织结构以提升逻辑清晰度和可扩展性
- 确保最终交付的代码达到高质量、高可维护性标准
