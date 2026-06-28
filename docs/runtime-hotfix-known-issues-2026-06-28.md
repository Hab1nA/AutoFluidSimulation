# 运行热修后遗留问题记录

> 记录日期：2026-06-28
> 范围：本次生产运行中 daemon 重启恢复、SSH 隧道/工作站控制、Fluent solver 续接相关问题
> 当前运行态：daemon、IPC、反向隧道和四台工作站 solver 已恢复运行；本文只记录尚未彻底修复的工程债和复发风险。

## 当前已恢复状态

- daemon 已受控重启并恢复：`autofluid-daemon.service` active，IPC `127.0.0.1:9527` 在线。
- 反向隧道已恢复：ocar 上 `127.0.0.1:2222/2223/2224/2225/2226` 均在监听。
- 工作站健康已通过主动检查刷新：WS-A、WS-B、WS-C、WS-D 均为 `ok`。
- 状态库中无 `Error` / `Paused` 步骤；`remote_tasks` 正好包含四个正在运行的 solver。
- 当前运行 solver：
  - WS-C：构型 `1006`
  - WS-D：构型 `1009`
  - WS-A：构型 `1011`
  - WS-B：构型 `1016`

## 尚未彻底修复的问题

### 1. 工作站 SSH 健康状态可能再次老化为 `unknown` / `stale`

**现象**

TUI 状态栏中的 `server_to_workstation_ssh` 可能在一段时间后从 `ok` 变为 `unknown`，各工作站详情显示为 `stale`。

**当前处理**

本次通过执行 `worker start` 触发主动 SSH readiness 检查，将四台工作站健康状态刷新为 `ok`。这没有中断当前 solver。

**未彻底修复的原因**

daemon dashboard 的后台健康检查是被动快照，不主动证明 SSH 仍可达。最后一次主动检查超过老化窗口后，健康状态会自然退化为 `stale`，聚合状态变为 `unknown`。

**风险**

任务实际正常运行时，TUI 仍可能显示工作站 SSH 状态未知，造成误判。

**彻底修复方向**

- 明确区分“被动快照已老化”和“主动探测失败”。
- dashboard 可以显示 `stale`，但不应把它等同于断连故障。
- 增加低频、低影响主动探测，或在 TUI 文案中显式说明状态来源。

### 2. daemon 受控重启与流水线停止的语义仍未完全分离

**现象**

受控重启 daemon 时，仍存在触发远程任务清理、清空 `remote_tasks` 或使 scheduler 误判远程任务丢失的风险。

**当前处理**

本次通过人工审计 DB、远端计划任务、Fluent 进程和 progress 文件，将可控任务接回流水线；对不可控 orphan Fluent 进行了清理，对可继续的 WS-C `1006` 保留并恢复轮询。

**未彻底修复的原因**

代码层面对“重启 daemon 并保留流水线”和“停止流水线并取消远程任务”的生命周期边界仍不够硬。daemon stop / service restart 路径仍可能调用偏向清理的逻辑。

**风险**

下次 daemon 重启时，可能再次出现：

- DB 中 `remote_tasks` 与工作站实际 Fluent 进程不一致。
- scheduler 将可恢复任务误判为 lost 后重复启动。
- 旧 Fluent 进程继续运行但 daemon 无法可靠控制。

**彻底修复方向**

- 在 daemon 生命周期中明确区分 restart、pause、stop、quit full。
- 只有显式停止流水线时才取消远程任务和清理远端 solver。
- daemon 启动时应先恢复 DB 中 remote task，再基于计划任务、pid、progress、flag 文件进行一致性判定。
- 增加覆盖 service restart 场景的回归测试。

### 3. WS-C 当前 `1006` 是旧 wrapper 任务，PID 文件缺失

**现象**

WS-C 构型 `1006` 的计划任务 `AutoFluid_e930510e3117` 正在运行，Fluent 日志和 progress 文件持续推进，但 `D:/xkz_1020/flags/autofluid_bg_e930510e3117.pid` 当前缺失。

**当前处理**

保留 WS-C `1006`，不重启、不清理。daemon 通过计划任务状态、Fluent 日志和 progress 文件继续判断其仍在运行。

**未彻底修复的原因**

该任务由热修前的旧 wrapper 启动，PID 文件生成和清理行为不可靠。后续新启动任务会走已热修的 wrapper / pid 检查路径，但当前正在运行的旧任务不能在不中断求解的情况下改造成新 wrapper。

**风险**

如果 WS-C `1006` 在完成前发生异常，daemon 对它的精确进程树控制能力弱于新 wrapper 任务。

**彻底修复方向**

- 等 `1006` 自然完成后，让后续任务使用新 wrapper。
- 对 legacy task 增加基于 schtasks、progress 和 Fluent 进程证据的只读恢复策略。
- 不应为了补 PID 文件而重启当前 WS-C solver。

### 4. orphan Fluent 防重复启动只做了局部热修

**现象**

本次故障中曾出现远程 wrapper / scheduled task 不再可控，但 Fluent 子进程仍在写旧 progress 的情况。若系统盲目认为任务 lost，会造成重复启动同一构型。

**当前处理**

已热修 `executor.remote_executor.query_remote_task_status()`：在任务条目或 PID 不完整时，会结合 pid file、计划任务、progress 文件和 Fluent 证据判断状态；对于疑似 orphan solver progress 的场景返回 `unknown`，避免直接重复启动。

**未彻底修复的原因**

该修复降低了重复启动风险，但还没有完整解决 daemon 重启时远程任务恢复的系统性问题。尤其是“旧任务不可控但仍运行”的场景仍需要人工或更强的恢复策略判定。

**风险**

极端情况下仍可能出现：

- 任务实际在运行，但控制证据不足。
- 任务已经 orphan，progress 仍存在旧数据。
- scheduler 在 `unknown` 状态下需要更明确的重试预算和最终处置策略。

**彻底修复方向**

- 统一 remote task 恢复状态机：`running`、`lost`、`unknown_orphan`、`completed`、`failed`。
- 对 `unknown` 状态设置可观测的重试预算和最终错误上报。
- 将 orphan Fluent 识别与清理策略从人工操作沉淀为受控命令或 daemon 恢复流程。

### 5. 工作树中还有 tunnel / SSH watchdog 相关改动未统一收敛

**现象**

当前工作树中除本次核心热修外，还有 tunnel supervisor / SSH watchdog 相关改动。

**当前处理**

本次只验证了与远程任务状态判断、SSH client、workstation tunnel watchdog 相关的目标测试，没有把所有相关改动作为一个完整架构修复提交。

**未彻底修复的原因**

这些改动横跨 PowerShell tunnel supervisor、SSH client 和测试，需要单独审查其设计边界，确认不会影响现有运行态。

**风险**

如果未整理就合并，可能把实验性或局部修复混入主线；如果完全不合并，隧道 supervisor 消失后的自恢复问题仍可能复发。

**彻底修复方向**

- 单独审查 `scripts/start_workstation_reverse_tunnel.ps1`、`utils/ssh_client.py` 和相关测试。
- 明确 tunnel supervisor 的所有权、重启策略、PID/日志证据和失败上报方式。
- 将可证明有效的部分整理为独立提交。

### 6. WS-C Windows 计划内重启后触发 Fluent/MPI 进程风暴

**现象**

WS-C 在凌晨 Windows 计划内重启后，`1008`、`1013`、`1014`、`1015` 等 solver 连续失败。远端 Fluent 日志显示后续任务启动时已有其他 Fluent 并行进程存在，工作站上曾残留多组 `cx2410.exe`、`ansyscl.exe`、`mpiexec.exe`、`hydra_pmi_proxy.exe`、`fl_mpi2410.exe`。

**已确认触发点**

WS-C 系统事件显示 `2026-06-29 00:26:44` 由 `NT AUTHORITY\SYSTEM` 发起计划内重启，`00:29:49` 事件日志服务重新启动。该重启导致前序 Fluent / scheduled task 状态进入不干净状态。

**当前处理**

- 已人工清理 WS-C 残留 Fluent/MPI 进程，并将失败构型重新置回可调度状态。
- 已热修 `executor.remote_executor.RemoteExecutor.wait_solver_completion()`：solver `.error`、输出缺失、startup `lost/failed/running` 无进度分支都会进入 `_kill_remote_task_for_config()`，从而触发远程任务终止和 Fluent/MPI 清理。
- 在不停止 daemon 的前提下，已在 ocar 启动运行态 sidecar 兜底守护：`/tmp/autofluid_solver_hotfix_guard.py`，日志 `/tmp/autofluid_solver_hotfix_guard.log`。它监控当前 `remote_tasks` 中的 solver 异常，发现 `.error` 或 startup 无进度异常时补做远程清理。
- 已将修复后的 `executor/remote_executor.py` 同步到 ocar 磁盘，但当前 daemon 进程未重启，内存中仍依赖 sidecar 兜底。

**未彻底修复的原因**

当前 daemon 不能无中断热加载 Python 模块。源码已修复并同步到服务器，但运行中的 daemon 只有在下次重启后才会自然加载新逻辑。sidecar 是本次不中断运行的临时保护层，不应长期替代 daemon 内部生命周期管理。

**风险**

- sidecar 只覆盖 solver 异常清理兜底，不负责调度状态机修复。
- 如果 sidecar 被误停，而 daemon 尚未重启加载新源码，旧内存逻辑仍可能在相同失败分支只清任务记录、不杀 Fluent/MPI。
- 调度器仍可能在单个工作站连续 solver 失败后继续派发后续构型，缺少工作站隔离 / backoff 策略。

**彻底修复方向**

- 在合适窗口受控重启 daemon，使 ocar 上已同步的 `executor/remote_executor.py` 生效，然后停止 sidecar。
- 为 solver 失败后工作站级 quarantine / backoff 增加调度策略，避免单机进程残留放大为队列级连锁失败。
- 扩展 Fluent/MPI 清理覆盖面，确保 evidence cleanup 能处理 MPI 子进程命令行证据不足的场景。
- 将本次新增回归测试纳入常规验证：solver `.error` 和 startup `lost` 都必须调用 `_kill_remote_task_for_config()`。

## 建议后续工作

1. 优先修复 daemon restart / stop 生命周期边界，防止下次受控重启再次影响 remote task。
2. 调整 dashboard 工作站 SSH 健康展示，避免把被动快照老化误报为未知故障。
3. 等当前四个 solver 自然完成后，观察后续新任务是否全部使用新 wrapper 并具备可靠 PID 文件。
4. 将 tunnel supervisor / watchdog 改动单独整理、审查、测试后再合并。
5. 在当前 solver 安全窗口内受控重启 daemon，让 WS-C Fluent/MPI 进程风暴修复从 sidecar 兜底切换为 daemon 内建逻辑。
