# E2E 冷启动问题记录（2026-06-14）

## 测试范围

本次测试按真实用户路径进行端到端冷启动：

1. 清理本地与远端残留运行进程和端口。
2. 启动本地 TUI Client。
3. 在 TUI 中依次输入 `daemon start`、`worker start`、`start`。
4. 持续监控 TUI、daemon、LocalWorker、Bridge 和状态接口日志。
5. 目标是监控到至少一个构型进入 solver 步骤。

测试结论：未达到真实 solver 运行态。流水线在 SC/Meshing 阶段失败，最终 engine 进入 `stopped`，所有构型的 solver 被标记为 `Error`。

## 运行环境调整

远端 ocar 的 `.env` 原本缺少 server 到 workstation 的 reverse tunnel reachable 配置，导致 `worker start` 在 daemon 侧检查原始 workstation 地址 `172.17.135.240:22` 并超时。

已按运行要求在远端 `.env` 增加：

```dotenv
AUTOFLUID_SSH_REACHABLE_HOST=127.0.0.1
AUTOFLUID_SSH_REACHABLE_PORT=2222
AUTOFLUID_SSH_CONNECTIVITY_MODE=reverse_tunnel
```

远端 `.env` 已保留备份：`.env.codex-e2e-backup-20260614-195945`。

## 问题 1：`daemon start` 冷启动路径未完成

现象：

- TUI 正常记录用户输入 `daemon start`。
- 120 秒内没有成功或失败的用户可见完成日志。
- 本地 IPC tunnel 被创建，但远端 daemon 未启动。
- IPC 协议探测返回连接异常，表现为 `ECONNRESET`。

影响：

- 用户按冷启动流程执行时，第一步就可能停在无明确反馈状态。
- TUI 日志不足以判断是远端 daemon 启动失败、SSH 命令卡住，还是 tunnel-only 状态。

建议排查：

- 检查 `autofluid-tui/src/daemon_mgr.rs` 中 `daemon start` 的远端启动等待路径。
- 确认 tunnel 建立成功后是否继续触发远端 `start_daemon.py`。
- 为 `daemon start` 增加明确阶段日志：tunnel started、remote command sent、remote pid detected、IPC protocol ready、timeout/failure reason。

## 问题 2：`worker start` 依赖远端 reachable env，缺失时误判工作站不可达

现象：

- 原始远端 `.env` 缺少 `AUTOFLUID_SSH_REACHABLE_HOST`、`AUTOFLUID_SSH_REACHABLE_PORT` 和 `AUTOFLUID_SSH_CONNECTIVITY_MODE`。
- `worker start` 已建立 reverse tunnel，但 daemon 仍检查 `172.17.135.240:22`。
- daemon 日志出现 `worker_start 失败，所有工作站 SSH 连通检查失败` 和 SSH timeout。

影响：

- reverse tunnel 实际可用时仍会被判定为 worker 不可启动。
- 用户侧看到的是 `worker_start` 超时或失败，根因不直观。

建议排查：

- daemon 启动时校验 server mode 必需的 reachable 配置。
- `worker start` 失败日志中同时输出实际检查的 host/port/connectivity mode。
- TUI 在启动 reverse tunnel 后，应能确认 daemon 侧最终使用的是 reachable host，而不是 TOML 原始 host。

## 问题 3：SC 阶段大量失败，阻止流水线进入 solver

现象：

- SW 阶段实际可以推进，构型 0-9 均完成 STEP 导出。
- SC 阶段开始后大量构型快速失败并重试。
- 多个 Bridge 日志显示 SpaceClaim GUI 启动或 ready 检查失败。
- 部分 Bridge 日志存在中文乱码，可读性不足。

影响：

- 下游 Meshing 被标记为 Error，BarrierMonitor 最终将所有 solver 标为 Error。
- 本次 E2E 无法达到真实 `solver=Running`。

建议排查：

- 检查 `bridge/SpaceClaimBridge.exe` 当前构建与日志编码设置。
- 检查 `engine/sc_process_pool.py` 对 Bridge ready 失败的错误透传，避免只返回 `ok=False` 而缺少可定位原因。
- 确认 SpaceClaim 并发槽位、启动等待时间和 GUI ready 文件写入逻辑是否适合当前机器。

## 问题 4：SW 收尾重复将已完成构型推入 SC 队列

现象：

- 构型 0 已经历 `SC=Error`、`Meshing=Error` 后，后续又被重新推入 SC 队列。
- 日志中在 SW 全部完成收尾时再次出现：
  - `构型0 已推入 SC 处理队列`
  - `构型1 已推入 SC 处理队列`
  - `构型2 已推入 SC 处理队列`
- 这些构型随后可能重新进入 `SC=Running`。

影响：

- 已失败构型被重复执行。
- 形成不合理状态组合：`meshing=Error` 但 `sc=Running`。
- 可能导致队列重复、状态回退和最终统计不可信。

建议排查：

- 检查 `engine/scheduler/sw_phase.py` 中 `_enqueue_server_mode_completed_sw()` 的调用时机。
- SW 单构型完成时已经入队，SW 汇总阶段不应再次入队已处理或已失败下游的构型。
- 入队前应检查 SC/Meshing/Solver 是否已处于终态，避免终态后重入队列。

## 问题 5：engine stopped 后仍接受在途 LocalWorker 结果并推进下游

现象：

- `20:15:26` BarrierMonitor 已将所有 solver 标记为 `Error`，engine 状态变为 `stopped`。
- `20:15:59` 仍收到构型 2 的 SC 完成结果。
- daemon 随后将构型 2 标记为 `SC=Completed`，并推入 Transfer 队列。

影响：

- 停止态后仍发生状态回写和队列推进。
- 可能造成 `engine=stopped` 但后台队列继续变化，TUI/daemon 状态不一致。
- 失败后的后续恢复、reset、retry 判断会变复杂。

建议排查：

- LocalWorker result handler 在写状态前应检查 engine/stopped/barrier failure 状态。
- `worker_step_complete` 对过期 task 或 stopped 后 task 应有丢弃、记录或隔离策略。
- Transfer/Meshing/Solver 入队逻辑应拒绝 stopped 后的新任务推进。

## 问题 6：日志噪声和可观测性不足

现象：

- daemon 主日志被高频 IPC client connect/disconnect 刷屏。
- LocalWorker 子任务执行期间没有足够进度日志，长时间只能看到 `sw=Running`。
- Bridge 日志存在乱码。
- `worker_step_complete` 只显示 `ok=True/False`，缺少具体失败原因。

影响：

- 真实问题定位需要跨多个日志源手动拼接。
- 用户侧难以区分“正常耗时”“卡住”“失败但未回传原因”。

建议排查：

- 降低 IPC connect/disconnect 日志级别，或为 dashboard polling 做去噪。
- LocalWorker 任务启动、完成、失败、超时应记录 step/config/task_id/duration/error。
- Bridge 和 SpaceClaim 日志统一 UTF-8。
- `worker_step_complete` 的失败结果应携带错误摘要。

## 本次测试关键证据

本地证据：

- TUI 日志：`logs/client/2026-06-14_20-00-34/autofluid-tui.log`
- node-PTY 事件：`%TEMP%/autofluid-e2e-driver-20260614-200034.jsonl`
- Bridge 日志：`logs/bridge/spaceclaim_bridge_slot1_*.log`

远端证据：

- daemon 输出：`/root/AutoFluidSimulation/logs/autofluid-daemon.out`
- 状态数据库：`/root/AutoFluidSimulation/data/pipeline_state_9f4bf5e5.db`

## 清理状态

测试结束后已停止本次启动的：

- 本地 TUI Client。
- LocalWorker。
- workstation/local-worker/IPC SSH tunnel。
- SolidWorks 与 SpaceClaim 相关进程。
- 远端 daemon。

本次记录只新增该 Markdown 文件；未修改 tracked 源码。
