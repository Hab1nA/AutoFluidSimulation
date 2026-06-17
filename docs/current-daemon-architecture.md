# 当前 Daemon 分布式架构说明

> 文档日期：2026-06-10  
> 适用范围：当前已落地的 daemon 拆分实现  
> 关联文档：`docs/architecture-refactoring-plan.md`

本文描述当前代码与实际部署中已经生效的 daemon 工作机制。重点是本次拆分任务涉及的部分：Linux 服务器上的 daemon、Windows 本地 PC 上的 `LocalWorker`、服务器到工作站的 SSH 连接，以及远程 Meshing/Solver 任务的恢复与清理逻辑。

## 1. 当前部署拓扑

当前架构是“三端协作”，但不是远期计划中的完整多工作站结果收集系统。

```text
┌──────────────────────────┐
│ 本地 Windows PC           │
│                          │
│ - Rust TUI / IPC Client   │
│ - LocalWorker             │
│ - SolidWorks / SpaceClaim │
└─────────────┬────────────┘
              │ JSON-over-TCP IPC
              │ worker_register / poll / heartbeat / step_complete
              ▼
┌──────────────────────────┐
│ ocar Linux Server         │
│                          │
│ - PipelineDaemon          │
│ - IPCServer               │
│ - StateManager SQLite     │
│ - PipelineScheduler       │
│ - RemoteExecutor          │
│ - server-side SCDOC cache │
└─────────────┬────────────┘
              │ SSH / SFTP
              │ reverse_tunnel: server -> localhost:2222 -> workstation
              ▼
┌──────────────────────────┐
│ Windows Workstation       │
│                          │
│ - SSH service             │
│ - ANSYS / Fluent          │
│ - schtasks background job │
│ - flag/log/pid files      │
└──────────────────────────┘
```

关键约束：

- 本地 PC 只负责 Windows-only 阶段：SolidWorks STEP 导出和 SpaceClaim SCDOC 转换。
- ocar daemon 负责持久调度、状态库、IPC 服务、远程传输、Meshing、Solver。
- ocar 到工作站的连接不依赖本地 PC。当前通过工作站主动建立的反向 SSH 隧道暴露给 ocar 使用。
- 当所有构型的 `sw` 和 `sc` 均已 `Completed` 后，server mode 允许 `LocalWorker` 离线，后续 `transfer / meshing / solver` 可以继续由 ocar 与工作站完成。

## 2. 主要组件职责

| 位置 | 组件 | 主要文件 | 职责 |
|------|------|----------|------|
| ocar | `PipelineDaemon` | `engine/daemon.py` | daemon 生命周期、IPC handler、server mode 启动校验、状态库选择、LocalWorker 注册与任务回收 |
| ocar | `IPCServer` | `ipc/server.py` | TCP JSON IPC 服务，注册控制命令和 worker 命令，支持 `AUTOFLUID_IPC_AUTH_TOKEN` |
| ocar | `StateManager` | `engine/state_manager.py` | SQLite WAL 状态库，保存步骤状态、工作站分配、远程任务元数据 |
| ocar | `PipelineScheduler` | `engine/scheduler/main.py` | SW/SC/Transfer/Meshing/Solver 调度、断点续传、自然完成收尾 |
| ocar | `TaskRunner` | `engine/task_runner.py` | 调度执行入口；server mode 下把 `sw/sc` 委托给 `LocalWorkerAdapter`，远程阶段交给 `RemoteExecutor` |
| ocar | `RemoteExecutor` | `executor/remote_executor.py` | SFTP、远程脚本同步、Meshing/Solver 启动、完成轮询、远程任务恢复 |
| ocar | `RemoteWorkstation` | `utils/ssh_client.py` | paramiko SSH/SFTP 封装，后台任务通过 Windows `schtasks` 启动 |
| 本地 PC | `LocalWorker` | `engine/local_worker.py` | 注册到 daemon、心跳、轮询本地任务、执行 SW/SC、上传 SCDOC |
| 本地 PC | `LocalWorkerAdapter` | `engine/local_worker_adapter.py` | daemon 侧任务投递适配器，等待 worker 回报结果 |
| 工作站 | wrapper 脚本/计划任务 | `utils/ssh_client.py` 生成 | 启动 Fluent/批处理脚本，写入 `done/error/log/pid` 标志文件 |

## 3. Daemon 启动机制

`PipelineDaemon.start()` 的关键步骤：

1. 加载配置和 `.env` 环境变量。
2. 读取 Excel 构型并计算构型指纹，选择 `pipeline_state_<fingerprint>.db`。
3. 在 `AUTOFLUID_SERVER_MODE=server` 且服务器上没有 Excel 时，不直接失败；如果存在已有非空指纹状态库，则恢复最近的状态库继续运行。
4. 创建 `StateManager`、`TaskRunner`、`PipelineScheduler`。
5. 创建 `LocalWorkerRegistry` 和 `LocalWorkerAdapter`。
6. 启动 `IPCServer`，注册普通控制命令和 worker 命令。

server mode 下有两个特殊边界：

- 如果是全新启动且还需要执行 `sw/sc`，必须先有 `LocalWorker` 在线。
- 如果状态库中所有构型的 `sw/sc` 已完成，daemon 不再强制依赖 `LocalWorker`，可以独立继续远程阶段。

## 4. LocalWorker 协议

`LocalWorker` 使用与 TUI 相同的 JSON-over-TCP IPC 协议连接 ocar daemon。

已实现的 worker 命令：

| 命令 | 方向 | 含义 |
|------|------|------|
| `worker_register` | LocalWorker -> daemon | 注册 worker、上报能力、网络信息和 Excel 构型 |
| `worker_heartbeat` | LocalWorker -> daemon | 周期性刷新在线状态 |
| `worker_poll` | LocalWorker -> daemon | 拉取一个待执行本地任务 |
| `worker_step_complete` | LocalWorker -> daemon | 上报 `sw/sc` 成功及结果 |
| `worker_step_error` | LocalWorker -> daemon | 上报 `sw/sc` 失败 |

执行流程：

1. 本地 PC 启动 `LocalWorker`，通过 `AUTOFLUID_SERVER_HOST`、`AUTOFLUID_IPC_PORT`、`AUTOFLUID_IPC_AUTH_TOKEN` 连接 ocar。
2. daemon 收到 `worker_register` 后，如果携带了构型数据，会初始化或切换到对应指纹状态库。
3. scheduler 需要执行 `sw/sc` 时，`TaskRunner` 在 server mode 下调用 `LocalWorkerAdapter`。
4. `LocalWorkerAdapter` 把任务放入 `LocalWorkerRegistry` 内存队列。
5. `LocalWorker` 通过 `worker_poll` 领取任务，在本地执行 SolidWorks 或 SpaceClaim。
6. SC 成功后，`LocalWorker` 读取生成的 SCDOC 并随 `worker_step_complete` 上传。
7. daemon 将 SCDOC 写入服务器本地 `LOCAL_PATHS["scdoc_dir"]`，server mode 默认路径在项目 `data/scdoc/` 下。

这意味着：SCDOC 一旦被 ocar 接收，后续 `transfer` 不再读取本地 PC 文件系统。

## 5. 流水线阶段分工

当前仍使用原有五阶段：

```text
SW -> SC -> Transfer -> Meshing -> Solver
```

| 阶段 | 当前执行位置 | 说明 |
|------|--------------|------|
| `sw` | 本地 PC `LocalWorker` | 依赖 SolidWorks COM，必须在 Windows 桌面环境执行 |
| `sc` | 本地 PC `LocalWorker` | 依赖 SpaceClaim/Bridge；完成后把 SCDOC 上传给 ocar |
| `transfer` | ocar -> 工作站 | ocar 使用 SFTP 将服务器本地 SCDOC 传到工作站 |
| `meshing` | 工作站 | ocar 通过 SSH 创建并运行 Windows 计划任务 |
| `solver` | 工作站 | Meshing 屏障满足后，通过同样的远程计划任务机制启动 Fluent |

当前没有要求本地 PC 作为 ocar 到工作站的跳板。实际连接路径必须是 ocar 自己能够访问的地址。

本地 PC 到 ocar 的 IPC 目前默认走本地 SSH 端口转发，而不是把 daemon 暴露到公网：

```text
127.0.0.1:19527 (local)
    -> ssh -L 127.0.0.1:19527:127.0.0.1:9527 ocar
    -> ocar daemon IPC 127.0.0.1:9527
```

因此本地启动脚本的职责是：

1. 从 `.env` 读取 `AUTOFLUID_SERVER_HOST=127.0.0.1`、`AUTOFLUID_IPC_PORT=19527`。
2. 用 `AUTOFLUID_SERVER_TUNNEL_HOST` / `AUTOFLUID_SERVER_TUNNEL_REMOTE_HOST` / `AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT` 自动建立 SSH 本地转发。
3. 再启动 `LocalWorker` 与 Rust TUI。

这条本地 SSH 隧道只服务于“本地 PC 访问 ocar daemon”，不参与“ocar 访问工作站”的链路，因此不违背 server mode 下“本地 PC 可以离线”的约束。

## 6. ocar 到工作站的连接机制

工作站配置由 `autofluid_config.toml`、`.env` 和环境变量共同决定，核心字段如下：

| 字段/环境变量 | 含义 |
|---------------|------|
| `remote_config.host` / `AUTOFLUID_SSH_HOST` | 工作站在内网中的原始地址 |
| `remote_config.port` / `AUTOFLUID_SSH_PORT` | 原始 SSH 端口 |
| `remote_config.reachable_host` / `AUTOFLUID_SSH_REACHABLE_HOST` | server mode 下 ocar 实际连接的地址 |
| `remote_config.reachable_port` / `AUTOFLUID_SSH_REACHABLE_PORT` | server mode 下 ocar 实际连接的端口 |
| `remote_config.connectivity_mode` / `AUTOFLUID_SSH_CONNECTIVITY_MODE` | 当前为 `reverse_tunnel` |
| `AUTOFLUID_SSH_PASSWORD` | ocar 连接工作站 SSH 所需密码 |

在 server mode 下，`engine.config._effective_workstation_config()` 会优先使用 `reachable_host/reachable_port` 覆盖 `host/port`。因此当前有效路径是：

```text
ocar -> reachable_host:reachable_port -> reverse SSH tunnel -> workstation sshd
```

当前部署中该模式用于解决“工作站在内网、ocar 不在内网”的问题。反向隧道应由工作站侧长期维持，不能依赖本地开发电脑在线。

## 7. 远程 Meshing/Solver 任务机制

`RemoteExecutor` 启动远程任务时，会通过 `RemoteWorkstation.exec_background()` 在工作站上创建 Windows 计划任务：

1. 在工作站 `flag_dir` 写入 `autofluid_bg_<hash>.cmd` wrapper。
2. wrapper 启动实际 Meshing/Solver 命令。
3. wrapper 写入 `autofluid_bg_<hash>.pid`，供超时/强制停止时精确终止进程树。
4. stdout/stderr 追加到 `autofluid_bg_<hash>.log`。
5. 成功时写入对应 `done` flag；失败时写入 `.error` flag。
6. wrapper 正常退出时删除自身计划任务和 PID 文件。

远程任务会同时写入两处状态：

- 内存：`RemoteExecutor._remote_tasks`
- SQLite：`remote_tasks` 表

`remote_tasks` 表保存：

- `workstation_id`
- `config_name`
- `step_name`
- `task_name`
- `flag_file`
- `error_flag_file`
- `log_file`
- `pid_file`
- `script_file`
- `started_at`

这些信息用于 daemon 重启后的恢复，避免 `meshing/solver` 已在工作站运行但 ocar 内存映射丢失后重复启动 Fluent。

## 8. 重启与断点续传

daemon 或 ocar 重启后，恢复逻辑主要分三层：

1. `PipelineDaemon` 在 server mode 无 Excel 时恢复最近的非空指纹状态库。
2. `RemoteExecutor.restore_remote_tasks_from_db()` 从 `remote_tasks` 表恢复内存映射。
3. scheduler 恢复扫描时，对 `Running` 的 `meshing/solver` 调用 `query_remote_task_status()`。

`query_remote_task_status()` 的判断顺序：

1. 检查 done flag，存在则视为 `completed`。
2. 检查 error flag，存在则视为 `failed`。
3. 查询 `schtasks` 是否仍存在。
4. 如果存在 PID 文件，则用 `tasklist` 二次确认实际进程是否仍在。
5. 根据结果返回 `running / completed / failed / lost / unknown`。

恢复策略：

- `completed`：标记步骤完成，并清理远程任务元数据。
- `failed`：标记步骤错误，并清理远程任务元数据。
- `running`：保持 `Running`，继续轮询，不重复启动。
- `lost`：确认无产物且远程任务丢失后，重置为 `Waiting` 再重新启动。
- `unknown`：保留 `Running`，避免误判导致重复启动。

## 9. 完成清理与强制终止的边界

当前代码刻意区分两类清理：

| 场景 | 使用接口 | 行为 |
|------|----------|------|
| 远程任务自然完成 | `cleanup_remote_task_entry()` | 删除计划任务条目和残留 PID 文件，不读取 PID，不杀进程树 |
| 超时或主动停止 | `kill_remote_task()` | 读取 PID 文件，`taskkill /PID <pid> /T /F` 终止进程树，再删除计划任务 |

这个边界很重要：自然完成时 wrapper 可能已经删除 PID 文件，如果复用强制终止路径，会产生误导性 PID 读取失败警告；而超时/停止时必须保留 PID 终止能力，避免残留 Fluent 进程。

## 10. 屏障与工作站分配

状态库中的 `steps.workstation_id` 保存构型到工作站的分配。当前配置仍可以只有一个默认工作站，但代码已经按工作站 ID 存储和查询：

- `StateManager.set_config_workstation()`
- `StateManager.get_config_workstation()`
- `StateManager.all_configs_completed_at_step(..., workstation_id=...)`
- `remote_tasks` 的唯一键为 `(workstation_id, config_name, step_name)`

Meshing 使用 `set_meshing_running_if_idle(config, workstation_id=...)` 做原子防护，确保同一工作站同一时刻只有一个构型进入 Meshing Running。

Barrier 侧已经支持工作站级屏障：

- 某工作站分配到的所有构型 Meshing 完成后，可以触发该工作站的 Solver 分发。
- 全部构型 Meshing 完成后，再设置全局 `global_barrier_met` 并做 SC 最终清理。

## 11. IPC 与安全边界

当前 IPC 协议仍是 JSON-over-TCP，请求/响应由 `ipc.protocol` 序列化。

server mode 部署时建议：

- `AUTOFLUID_IPC_HOST` 绑定到需要暴露的地址。
- `AUTOFLUID_IPC_AUTH_TOKEN` 必须设置，TUI 与 LocalWorker 请求都需要携带同一 token。
- 工作站 SSH 密码只放在 ocar 的 `.env` 或运行环境中，不写入文档和仓库。
- ocar 到工作站的 SSH route 必须从 ocar 机器上验证，不以本地 PC 可连通作为依据。

## 12. 服务器 CLI 与 OpenClaw 告警

ocar 上提供轻量服务器 CLI，用于无图形环境和 OpenClaw 调用：

```bash
.venv/bin/python -m tools.autofluid_cli status
.venv/bin/python -m tools.autofluid_cli start
.venv/bin/python -m tools.autofluid_cli pause
.venv/bin/python -m tools.autofluid_cli check
.venv/bin/python -m tools.autofluid_cli worker restart
.venv/bin/python -m tools.autofluid_cli daemon restart
```

CLI 默认输出 JSON，并复用 `AUTOFLUID_IPC_HOST`、`AUTOFLUID_IPC_PORT` 和
`AUTOFLUID_IPC_AUTH_TOKEN`。`daemon start|stop|restart|status` 默认控制
systemd 服务 `autofluid-daemon`，可用 `AUTOFLUID_DAEMON_SERVICE` 覆盖。

服务器 CLI 的 `clean/reset` 权限比 TUI 更窄：它会拒绝任何影响 `sw`、`sc`
或 `all` 的操作，避免 ocar 侧命令改写 LocalWorker 持有的 SolidWorks /
SpaceClaim 本地状态或文件。服务器 CLI 只允许远程侧步骤，例如
`transfer`、`meshing`、`solver` 和远程缓存清理。

OpenClaw 告警 watcher 由 server-mode daemon 拥有生命周期：daemon 在 IPC
启动成功后拉起 watcher，关闭时先终止 watcher 再关闭 IPC，避免服务器侧留下
孤立告警进程。生产环境通过 `.env` 配置 webhook：

```bash
AUTOFLUID_OPENCLAW_WEBHOOK_URL=http://127.0.0.1:8080/autofluid
AUTOFLUID_OPENCLAW_WEBHOOK_TOKEN=
```

watcher 通过 IPC 拉取 `WARNING` 及以上日志，按 `level + source + raw_message`
生成指纹，默认 10 分钟内同一指纹只通知一次。Webhook 使用 `POST` JSON，
字段包括 `title`、`level`、`message`、`source`、`timestamp`、`log_id`
和 `fingerprint`；如设置 `AUTOFLUID_OPENCLAW_WEBHOOK_TOKEN`，请求会携带
`Authorization: Bearer <token>`。

生产环境只保留 daemon 内部托管 watcher 这一条启动路径。不要启用独立的
`autofluid-alert-watcher.service`，否则可能与 daemon 拥有的 watcher 重复拉取
同一批日志并重复发送告警。ocar 上该独立 systemd unit 应保持 masked；如需恢复，
必须先确认 daemon 内部 watcher 已停用或同步调整生命周期设计。

## 13. 当前未完成的远期项

以下内容属于 `architecture-refactoring-plan.md` 中的远期规划，不应误认为当前已经完整落地：

- 多台工作站的生产级调度策略和容量治理。
- `PostProcess` 独立阶段。
- `ResultCollector` / `ResultFetcher` 自动结果回收。
- 服务器暂存区到本地归档目录的离线补收机制。
- TUI 中完整的 LocalWorker/工作站管理 UI。

当前已经落地的是：daemon 迁移到 ocar 后，可以通过 `LocalWorker` 获得本地 SW/SC 能力，并在本地 PC 离线后继续通过 ocar 与工作站推进远程阶段。

## 14. 运维检查要点

排障时优先确认以下状态：

1. ocar daemon 是否运行最新代码，IPC 端口是否监听。
2. ocar 环境是否设置 `AUTOFLUID_SERVER_MODE=server`。
3. 本地启动失败时，检查 `AUTOFLUID_SERVER_TUNNEL_HOST=ocar` 对应 SSH 配置是否可无交互登录，以及 `127.0.0.1:19527` 隧道是否已建立。
3. `LOCAL_PATHS["scdoc_dir"]` 在 server mode 下是否指向服务器本地可写目录。
4. `reachable_host/reachable_port/connectivity_mode` 是否是 ocar 可达路径。
5. 工作站反向隧道是否由工作站侧长期维持。
6. `remote_tasks` 表是否只保留正在运行的 Meshing/Solver 任务。
7. 工作站 `flag_dir` 下对应任务的 `.log/.pid/done/.error` 是否符合当前步骤状态。
