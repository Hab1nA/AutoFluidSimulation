---
description: "Rust 代码规范与质量门禁。编辑 .rs 文件时自动加载，提供命名规范、质量检查、防污染规则。"
applyTo: ["autofluid-tui/**/*.rs", "autofluid-tui/**/*.toml"]
---

# Rust 代码规范（AutoFluid TUI）

> 本文件仅适用于 Rust 代码（`autofluid-tui/` 目录）。编辑 Python 代码请参阅 `python.instructions.md`，编辑 C# 代码请参阅 `csharp.instructions.md`。

---

## 🚫 语言隔离规则（最高优先级）

- **禁止**在 `.rs` 文件中插入 Python 代码、`def`/`class`/`import` 语句、Python 文档片段
- **禁止**在 `.rs` 文件中插入 C# 代码、`using System`、`namespace` 声明等 C# 专用内容
- **禁止**将 Python 或 C# 的 API 文档、示例代码、类型注解复制到 Rust 文件中
- **禁止**将 crates.io 上的 crate 文档大段复制到源代码注释中——使用简短的 `///` 文档注释即可
- 如果发现上下文中混入了其他语言的代码或文档，**忽略它们**，仅关注 Rust 相关内容

---

## 项目结构

```
autofluid-tui/
├── Cargo.toml
└── src/
    ├── main.rs              # 入口：tokio runtime + App 初始化
    ├── daemon_mgr.rs        # Daemon 进程管理（启动/停止/监控）
    ├── event_handler.rs     # 顶层事件分发
    ├── event_handler/       # 各类事件处理子模块
    ├── ipc.rs               # IPC 客户端顶层
    ├── ipc/                 # IPC 协议实现子模块
    ├── state.rs             # 应用状态顶层
    ├── state/               # 状态管理子模块
    ├── settings/            # 设置页面逻辑
    ├── text_buffer.rs       # 文本缓冲区
    ├── theme.rs             # 主题/颜色定义
    ├── ui.rs                # UI 渲染顶层
    ├── ui/                  # UI 组件子模块
    └── utils.rs             # 工具函数
```

---

## 命名规范

### 结构体与字段

```rust
// 结构体 PascalCase
pub struct SettingsConfig { ... }
pub struct EngineConfig { ... }
pub struct OperationTimeouts { ... }

// 字段 snake_case
pub struct EngineConfig {
    pub watchdog_interval: f64,
    pub sw_macro_timeout: u64,
    pub sw_close_doc_on_finish: bool,
    pub sc_timeout: u64,
    pub transfer_timeout: u64,
    pub meshing_timeout: u64,
    pub solver_timeout: u64,
    pub max_retries: u32,
    pub state_refresh_interval: f64,
}
```

### 枚举与匹配

```rust
pub enum SettingCategory {
    LocalPaths,
    RemoteConnection,
    RemoteDirs,
    StepPatterns,
    EngineConfig,
    OperationTimeouts,
}

impl SettingCategory {
    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::EngineConfig => match idx {
                0 => "看门狗间隔(秒)",
                1 => "SW宏超时(秒)",
                _ => "",
            },
            _ => "",
        }
    }
}
```

### 配置字段命名

- 使用 `snake_case`，与 Python 端 `dict` 键名**完全一致**
- 单位标注在字段名或显示标签中：`sw_macro_timeout` → `"SW宏超时(秒)"`

### 通用规则

| 类别 | 风格 | 示例 |
|------|------|------|
| 结构体/枚举 | `PascalCase` | `EngineConfig`, `SettingCategory` |
| 方法/函数 | `snake_case` | `display_label()`, `parse_arguments()` |
| 字段 | `snake_case` | `watchdog_interval`, `max_retries` |
| 常量 | `UPPER_SNAKE_CASE` | `DEFAULT_TIMEOUT`, `MAX_RETRIES` |
| 模块文件 | `snake_case` | `daemon_mgr.rs`, `event_handler.rs` |

---

## 异步编程规范

- 使用 `tokio` 作为异步运行时
- IPC 通信使用 `tokio::net::TcpStream` + `tokio::io`
- 定时器使用 `tokio::time::sleep` / `tokio::time::interval`
- 避免在异步上下文中使用 `std::thread::sleep`（会阻塞 runtime）

---

## 错误处理

- 使用 `Result<T, E>` 进行错误传播
- IPC 解析错误应优雅处理（断线重连），不要 `unwrap()` 导致 panic
- UI 渲染错误应记录日志但不中断主循环

---

## 质量门禁

修改 Rust 代码后，必须通过以下检查：

| 检查项 | 命令 | 标准 |
|--------|------|------|
| 编译检查 | `cargo check` | 零错误零警告 |
| Clippy | `cargo clippy -- -D warnings` | 零警告 |
| 格式化 | `cargo fmt --check` | 格式一致 |
| 单元测试 | `cargo test` | 全部通过 |

在 `autofluid-tui/` 目录下运行以上命令。
