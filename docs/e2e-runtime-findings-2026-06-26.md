# E2E Runtime Findings - 2026-06-26

## Current run

- Final daemon session: `/root/AutoFluidSimulation/logs/server/sessions/daemon/2026-06-26_22-38-49`
- Active database: `/root/AutoFluidSimulation/data/pipeline_state_e39187fa.db`
- Final result:
  - `solver=Completed`: 10/10
  - `postprocess=Completed`: 10/10
  - final remaining task was `config 5` on `WS-C`.
- Final shutdown:
  - `quit full` was executed through the release TUI client at
    `2026-06-27 19:22:59`.
  - The daemon log records `收到 full_quit 命令`, process lock release, and
    `PipelineDaemon 已关闭` at `2026-06-27 19:23:00`.
  - Local IPC probes to `127.0.0.1:19527` and `127.0.0.1:9527` were refused
    after shutdown.
  - `ocar` had no listeners on `2222`, `2223`, `2224`, `2225`, or `9527`.
  - The final DB had no `remote_tasks` rows.
- Solver timeout is `28800s` (8 hours); postprocess timeout is `14400s` (4 hours).

## Finding 1: Solver remaining-time display is single-config only

Observed behavior:

- TUI showed solver remaining time only for `WS-B` / `config 2`.
- `WS-A` / `config 1` and `WS-C` / `config 3` stayed as `Running`.
- Dashboard payload contained only one `engine.solver_progress` object:
  `{"config_name": 2, "current_iter": 438, "total_iter": 1000, ...}`.

Root cause:

- `StateManager.set_solver_progress()` stores progress under one global
  `engine_state` key named `solver_progress`.
- Each solver polling thread calls `_read_solver_progress()` for its own
  remote `solver_progress_<config>.json`, then overwrites the same global key.
- The Rust TUI also models this as `Option<SolverProgress>` and renders a
  countdown only when the row config matches that one global object.

Impact:

- Concurrent solver runs cannot all display remaining time.
- The visible countdown depends on whichever solver progress was last read and
  successfully written to daemon state.

Required follow-up:

- Replace the global `solver_progress` object with a per-config map, for example
  `solver_progress_by_config`.
- Keep or migrate the old single-object key only if backward compatibility is
  still required.
- Update Python dashboard payload, Rust parsing/rendering, and tests to verify
  multiple simultaneous solver countdowns.

## Finding 2: WS-A and WS-C reverse SSH tunnels are down while health reported ok

Observed behavior:

- `get_dashboard` reported `workstation_ssh_details` as `WS-A=ok`,
  `WS-B=ok`, `WS-C=ok`.
- Direct check on `ocar` showed only `127.0.0.1:2224` listening; `2222` and
  `2225` were not listening.
- Direct SSH from `ocar` to `127.0.0.1:2222` and `127.0.0.1:2225` failed with
  connection refused.
- Daemon logs repeatedly show SSH connection failures for ports `2222` and
  `2225`.

Impact:

- The health panel can be stale or misleading during active runs.
- The daemon cannot read live progress files from `WS-A` and `WS-C`, which also
  explains why their solver countdowns are not reaching the TUI even before the
  single-progress overwrite issue is fixed.

Required follow-up:

- Rework health calculation so each workstation status is based on fresh
  connectivity or a clearly timestamped stale state.
- Consider separating active task ownership from live SSH reachability in the
  dashboard.
- The daemon's SSH reconnect path only reconnects Paramiko to the configured
  endpoint. It does not rebuild the local `ssh -R` tunnel when the remote port
  itself has disappeared.
- The workstation tunnel monitor has a bounded recovery budget
  (`MaxConsecutiveFailures=5`, `MaxRecoverySeconds=120`). After that, it can
  stop trying while the daemon continues to report the last active check.
- Add a daemon/client recovery path that can rerun the tunnel entry script, or
  make the monitor/watchdog continue recovering while the owner marker is alive.
- Treat a live tunnel supervisor process without a matching `ssh -N -R`
  child/remote listening port as unhealthy.

Manual recovery performed:

- Rebuilt the missing workstation reverse tunnels without resetting pipeline
  state:
  - `WS-A`: `127.0.0.1:2222 -> 172.17.135.240:22`
  - `WS-C`: `127.0.0.1:2225 -> 172.17.135.115:22`
- Local monitor PIDs after recovery:
  - `data/tunnel_workstation_2222.pid`: `20304`
  - `data/tunnel_workstation_2225.pid`: `31408`
- Local `ssh -N -R` child PIDs after recovery:
  - `15688`: `-R 127.0.0.1:2222:172.17.135.240:22 ocar`
  - `42468`: `-R 127.0.0.1:2225:172.17.135.115:22 ocar`
- `ocar` listener check after recovery showed all expected server-side ports
  listening: `2222`, `2223`, `2224`, `2225`, and `9527`.
- Daemon-side SSH evidence after recovery:
  - before recovery, `engine.task_runner.log` repeatedly reported
    `SSH 重连失败: WS-A` and `SSH 重连失败: WS-C`;
  - before recovery, `utils.ssh_client.log` repeatedly reported
    `Unable to connect to port 2222/2225 on 127.0.0.1`;
  - after recovery, `utils.ssh_client.log` reported successful connections to
    `ps@127.0.0.1:2222` at `2026-06-26 19:14:50` and
    `bh@127.0.0.1:2225` at `2026-06-26 19:15:04`.
- Direct `ssh ocar 'ssh -p 2222 ps@127.0.0.1 ...'` / `2225 bh@...`
  probes failed with public-key/password authentication errors because the
  command was non-interactive and did not use the daemon's password handling;
  this is not evidence of daemon failure. The daemon log confirms authenticated
  project-path connections succeeded after the tunnel ports were restored.
- The tunnel startup script also printed scheduled-task registration
  `拒绝访问`; the live tunnels still started. Record this as a watchdog
  persistence permission issue to fix separately.
- A later WS-C recurrence happened while `config 5` was running:
  - dashboard still reported `WS-C=ok`;
  - `ocar` no longer had `127.0.0.1:2225` listening;
  - the old local monitor was alive but had no real `ssh -N -R` child, only
    stale TCP probe processes;
  - restarting the tunnel script first wrote PID `48140` but the process did
    not stay alive, and `2225.err.log` showed
    `client_loop: send disconnect: Connection reset`.
- Manual recovery for that recurrence:
  - stopped only stale 2225 TCP probe `ssh.exe` processes;
  - restarted `scripts/start_workstation_reverse_tunnel.ps1` for
    `127.0.0.1:2225 -> 172.17.135.115:22`;
  - new supervisor PID was `41736`, with child `ssh.exe` PID `48580`;
  - functional server-side probe to `127.0.0.1:2225` succeeded afterward.
- This confirms the durable issue is not only a missing initial tunnel; the
  monitor can remain misleadingly alive after the effective reverse tunnel is
  gone, and automatic recovery can exhaust or fail without surfacing that as
  workstation health failure.

## Finding 3: Stale Fluent processes can survive previous runs

Observed behavior on `WS-B`:

- Current active solver is task `AutoFluid_9c2597f4dd7e`, wrapper PID `162608`,
  running `batch_solver_gen4.py 2` with `--processor-count 128`.
- Stale processes were also present:
  - two old one-core PyFluent Fluent/Cortex trees (`fluent.exe ... -t1`)
  - one old eight-core MPI/meshing residue (`mpiexec.exe -n 8` and
    `hydra_pmi_proxy.exe`)

Manual cleanup performed:

- Killed only confirmed stale root PIDs on `WS-B`: `174100`, `170852`,
  `162200`, `160456`.
- Verified current solver progress continued afterward:
  `config 2`, `current_iter 464/1000`, `remaining_sec 5960`.
- Later inspection on `WS-C` showed two concurrent 128-core Fluent/MPI trees:
  an old tree started around `2026-06-26 01:50` and the current `config 3`
  tree started around `2026-06-26 17:43`.
- `config 3` had no `solver_progress_3.json`; its log stopped after Fluent
  mesh loading:
  `DESKTOP-96BINDP is already loaded (100). Process affinity not being set.`
- Killed only the confirmed old `WS-C` roots:
  - `28312`: old `fluent.exe ... -t128`
  - `42640`: old `mpiexec.exe -n 128`
  - `32856`: old `hydra_pmi_proxy.exe`
- Verified the current `WS-C` solver tree remained alive afterward:
  wrapper PID `34948`, Python PID `60052`, Fluent PID `2152`, `mpiexec`
  PID `59340`, with one active `fl_mpi2410.exe=128` set.
- After cleanup and a two-minute wait, dashboard began reporting
  `config 3` solver progress: `current_iter 2/1000`, `remaining_sec 21756`.
- `WS-A` later entered `postprocess` for `config 1` at about
  `2026-06-26 20:03`.
- The `config 1` background log shows the intended same-session handoff:
  after `[1] Solver 完成标志已写入`, the same
  `D:/xkz_1020/flags/autofluid_bg_891e9853c47b.log` immediately continues
  with `[1] 正在执行后处理 Journal: ...solver_post_gen4.jou` and
  `Reading journal file ...solver_post_gen4.jou`.
- Daemon-side executor log also confirms the handoff:
  `构型1 仿真求解完成（cas+dat 均已保存）` followed by
  `构型1 已接管 Solver Fluent 会话，等待后处理完成`.
- There was no second `正在启动 Fluent Solver...` line for postprocess in
  that log segment, so the observed runtime path matches the design goal:
  postprocess takes over the solver Fluent session instead of launching a new
  Fluent process and rereading case/data.
- `config 1` postprocess completed normally:
  - executor log start: `2026-06-26 20:02:04`
  - executor log completion: `2026-06-26 20:11:40`
  - elapsed time: about 9 minutes 36 seconds
  - background log contains `五项指标后处理完成` and
    `PostProcess 完成标志已写入`.
- After `config 1` postprocess completed, `WS-A` immediately launched
  `config 4` solver as remote task `AutoFluid_5c8b77368e8d`; the progress
  file later showed `current_iter 30/1000`.
- `WS-A` also had four stale one-core/server-info Fluent processes unrelated
  to current `config 1`, created at `2026-06-24 05:00`,
  `2026-06-24 17:18`, `2026-06-26 05:28`, and `2026-06-26 13:22`.
  Killed only those stale roots: `96532`, `69292`, `107148`, `27968`.
- Verified after cleanup that `WS-A` kept only the active current task
  processes: `fluent.exe 129824` and `mpiexec.exe 27128`.

Root cause hypothesis:

- Normal successful solver path calls `close_solver_session()`, which attempts
  `solver_session.exit()` and then `force_exit()` if available.
- Timeout/stop cleanup uses `kill_remote_task()`, but this is based on the
  scheduled wrapper PID file and `taskkill /PID <wrapper> /T /F`.
- PyFluent/Fluent can create Cortex/MPI processes whose parentage is no longer
  under the recorded wrapper PID, so `taskkill /T` may miss detached Fluent
  descendants.

Required follow-up:

- Add a remote Fluent cleanup strategy that records and terminates the specific
  Fluent server-info/session process family for each AutoFluid task, not just
  the wrapper PID.
- Avoid image-name-only cleanup while concurrent solver tasks may be running.
- Add tests around `kill_remote_task()` to prove detached Fluent/Cortex/MPI
  cleanup uses task-specific evidence.

## Timeout note

- The configured solver timeout is 8 hours, so expected `WS-B` / `WS-C` solver
  durations around 4.5 hours should not be falsely timed out by the main solver
  watchdog.
- The postprocess timeout is 4 hours, well above the expected 15-20 minute
  postprocess duration. If postprocess approaches this timeout, treat it as a
  real fault rather than expected slow execution.

## Finding 4: S->L can show disconnected after worker restart even when tunnel is alive

Observed behavior:

- Dashboard showed `local_worker_online=true` but
  `server_to_local_ssh=disconnected`.
- Direct server-side checks showed the local-worker reverse tunnel was alive:
  `ocar` had `127.0.0.1:2223` listening and a socket connection to that port
  succeeded.
- Local processes were also alive:
  - LocalWorker process: `main.py --worker`, PID `42208`
  - local-worker tunnel monitor: PID `28032`
  - SSH reverse tunnel child: PID `8504`,
    `-R 127.0.0.1:2223:127.0.0.1:22 ocar`

Root cause:

- The initial `worker_register` at `2026-06-26 16:18:56` included the expected
  network metadata: `reachable_host=127.0.0.1`, `ssh_port=2223`,
  `connectivity_mode=reverse_tunnel`.
- At `2026-06-26 16:21:16`, the client sent `worker_restart`.
  `handle_worker_restart()` runs `worker_stop` followed by `worker_start`.
- `handle_worker_start()` intentionally calls
  `local_worker_registry.clear_online_workers()` after checking workstation SSH,
  so the previous LocalWorker registration metadata is erased.
- The already-running LocalWorker then continued to send only
  `worker_heartbeat` / `worker_poll` with `{"worker_id": "Spica"}`.
  `LocalWorkerRegistry.heartbeat()` can recreate a minimal online entry if the
  registry is empty, but that minimal entry has `network={}`.
- `_build_health_snapshot()` does not probe `127.0.0.1:2223`; it reports
  `server_to_local_ssh=ok` only when an online worker snapshot contains both
  `network.reachable_host` and `network.ssh_port`. With the minimal heartbeat
  entry, it reports `disconnected` even though the actual tunnel is alive.

Manual recovery performed for the active E2E run:

- Sent a non-restarting `worker_register` IPC request for the existing
  LocalWorker `Spica` with:
  `reachable_host=127.0.0.1`, `ssh_port=2223`,
  `connectivity_mode=reverse_tunnel`.
- Verified immediately afterward that dashboard health changed to:
  `local_worker_online=true`, `server_to_local_ssh=ok`,
  `server_to_workstation_ssh=ok`.

Code fix implemented locally:

- `LocalWorker.build_heartbeat_request()` now includes the worker's
  `capabilities` and `network` metadata.
- `PipelineDaemon.handle_worker_heartbeat()` forwards optional
  `capabilities`, `network`, and `remote_addr` into the registry.
- `LocalWorkerRegistry.heartbeat()` can update or rebuild worker metadata from
  heartbeat payloads, so clearing the registry during `worker_start/restart`
  no longer leaves S->L permanently metadata-less.
- Focused tests passed:
  `tests/test_local_worker.py::test_local_worker_builds_register_and_heartbeat_requests`,
  `tests/test_local_worker_registry.py::test_heartbeat_can_rebuild_worker_metadata_after_registry_clear`,
  `tests/test_daemon_dashboard.py::test_dashboard_health_reports_server_to_local_from_online_worker`.

## Finding 5: Postprocess metrics used a hard-coded fluid cell zone name

Observed behavior:

- `config 2` solver completed normally on `WS-B`, and the same Fluent session
  successfully ran the video postprocess journal:
  - solver completion: `2026-06-26 20:43:50`
  - same-session postprocess handoff:
    `构型2 已接管 Solver Fluent 会话，等待后处理完成`
  - both animation videos were exported and moved to
    `D:\xkz_1020\animation\v_gen4_2.mp4` and
    `D:\xkz_1020\animation\t_gen4_2.mp4`.
- The failure happened during the five-metric collection phase:
  `postprocess_metrics_gen4.py::_run_report()` called Fluent's
  `volume_integrals.volume_integral(...)` for `qdot_actual`.
- Fluent raised:
  `Values contain disallowed entries; Error Object: (s------6.5076)`.
- The `config 2` case file was scanned directly on `WS-B`. It contains
  `s------6.5084` and `interior--s------6.5084`, not the hard-coded
  `s------6.5076` used by the script.

Root cause:

- `executor/remote_scripts/postprocess_metrics_gen4.py` hard-coded
  `FLUID_ZONE = "s------6.5076"`.
- Fluent can assign a different generated suffix for the fluid cell zone per
  case/configuration, so the hard-coded name is not portable across all 10
  configurations.

Code fix implemented locally:

- Replaced the fixed fluid zone with runtime discovery from the active Fluent
  session:
  `solver.settings.setup.cell_zone_conditions.fluid.get_object_names()`.
- The discovered fluid zone is now used for both:
  - `volume_integrals.volume_integral(..., cell_zones=[fluid_zone])`
  - named-expression volume integrals such as
    `Sum(..., ['<fluid_zone>'], Weight="Volume")`.
- Added a focused test for a case-specific zone name such as `s------6.5084`.
- Focused tests passed:
  `tests/test_compute_metrics_script.py::test_resolve_fluid_cell_zone_uses_case_specific_fluent_name`,
  `tests/test_compute_metrics_script.py::test_parse_report_file_accepts_single_zone_and_net_rows`.

Runtime mitigation for the active E2E run:

- Follow-up check on 2026-06-26 found the server checkout and all three
  workstation runtime script directories still carried the old hard-coded
  script (`FLUID_ZONE = "s------6.5076"` and no `_resolve_fluid_cell_zone`).
- This means the fixed local script must be explicitly resynced to
  `/root/AutoFluidSimulation/executor/remote_scripts/postprocess_metrics_gen4.py`
  and to each workstation's `D:/xkz_1020/scripts/postprocess_metrics_gen4.py`
  before retrying config 2 postprocess or allowing later solver tasks to enter
  the metrics phase.
- Resync completed after the follow-up check:
  - server SHA256:
    `ee8859a01e834fad929f8ae2d6af685b144b12c20d2617b212f8c36f8087b6b1`
  - `WS-A`, `WS-B`, and `WS-C` runtime script MD5:
    `c0441841e1988259513ae0063e8e22e4`
  - all three workstation copies now contain `_resolve_fluid_cell_zone` and
    use `cell_zones=[fluid_zone]`.
- The script still keeps `DEFAULT_FLUID_ZONE = "s------6.5076"` only as a
  final fallback when Fluent's settings API cannot enumerate fluid cell zones;
  the normal path is runtime discovery from the active case.

## Finding 6: paused `start`/`resume` can deadlock while dispatching postprocess backlog

Observed behavior:

- After `pause`, `reset_step config=2 step=postprocess`, `clean_step config=2 step=postprocess`, the first `start` IPC request timed out.
- The old daemon log stopped after checking running solver tasks and then writing only:
  `全局屏障已通过，检查待分发 Solver 任务`.
- `engine_status` remained `paused`; a second `start` logged only `收到继续指令`, consistent with another IPC handler waiting behind the same resume transition.

Root cause:

- `PipelineScheduler.resume()` entered `PipelineControl.resume_transition()` while the old paused event was still set.
- Inside that transition it called `_resume_paused_steps()`.
- `_resume_paused_steps()` can call `barrier_coordinator.dispatch_solver_if_ready()` for a Waiting postprocess backlog.
- `dispatch_solver_if_ready()` reaches `PauseGuard.check_should_abort()`, which waits for the paused event to clear.
- The paused event was only cleared after `_resume_paused_steps()` returned, so the resume path could wait on itself.

Runtime recovery:

- A diagnostic `SIGQUIT` attempt did not emit a usable Python stack and caused the system-owned daemon to restart.
- After restart, a normal IPC `start` succeeded from persisted state.
- Verified new daemon session `2026-06-26_21-32-42` is `running`; config 2 postprocess is Running, and config 3/4/7 solver tasks were recovered as remote-running instead of restarted.

Code fix implemented locally:

- `PipelineControl.resume_transition()` now clears the old paused event before yielding to resume reconciliation, while still holding the transition lock so a new concurrent pause waits and wins after resume reconciliation completes.
- Added `tests/test_pipeline_control.py::test_resume_transition_opens_pause_gate_for_reconciliation`.
- Focused tests passed:
  `tests/test_pipeline_control.py`, and the scheduler resume guard tests selected with `pytest -k`.

## Finding 7: restart recovery allowed same-workstation postprocess while solver was still running

Observed behavior:

- After daemon restart, config 2 postprocess was retried on `WS-B` while config 7 solver was still running on `WS-B`.
- The independent postprocess opened `model_gen4_2.cas.h5`, started the GUI postprocess journal, then failed at:
  `cx-name-to-id: cannot find widget: Video Options*Table1*IntegerEntry2(FPS)`.
- The same log showed cleanup failures for shared files under `D:\xkz_1020\workingdir`, and a live directory listing showed `animation-t` and `animation-v` frames still being written by the active WS-B solver.

Root cause:

- Daemon restart loses in-memory solver dispatcher threads and their live workstation map.
- The new scheduler saw config 2 as a postprocess backlog and config 7 as `solver=Running` on the same workstation.
- `_next_solver_config()` prioritized completed-solver postprocess backlog before recovering `Running` solver rows, so it launched independent postprocess on a workstation that was already occupied by an active solver.
- The two tasks share workstation-scoped Fluent working directories, so concurrent execution is unsafe.

Code fix implemented locally:

- `BarrierCoordinator._next_solver_config()` now first returns any `Running` solver or `Running` postprocess in the allowed workstation scope before selecting postprocess backlog or new solver tasks.
- Added regression test:
  `tests/test_scheduler_modules.py::TestBarrierCoordinator::test_running_solver_on_same_workstation_preempts_postprocess_backlog_after_restart`.
- Focused tests passed for running-solver recovery and postprocess backlog ordering.

Runtime handling for active E2E:

- Config 2 postprocess remains `Error` for now.
- Do not reset/retry config 2 while config 7 is still running on `WS-B`; retry it after WS-B becomes idle, using only `reset_step/clean_step/start` client/IPC commands.

Follow-up runtime correction:

- After the duplicate-task incident, the original solver control processes for
  configs 3, 4, and 7 were no longer recoverable:
  - their original `autofluid_bg_*.pid` files still existed,
  - `tasklist /FI "PID eq <pid>"` reported no matching task,
  - `schtasks /Query /TN <original task>` reported the task was missing,
  - the original logs ended with `^C`/interrupted batch evidence,
  - Fluent/MPI child processes were still left behind on the workstations.
- Those Fluent/MPI processes are orphaned from the Python batch script, so they
  cannot reliably write `solver_done_*` / `postprocess_done_*` flags or run the
  intended same-session postprocess handoff.
- Recovery policy for this E2E run: terminate only these orphan Fluent/MPI
  processes, then use client/IPC `reset_step` and `clean_step` from `solver`
  onward for the affected configurations instead of resetting all earlier SW,
  SC, transfer, and meshing work.

## Finding 8: Completed output validation treated unknown remote stat as missing

Observed behavior:

- After the orphan-process recovery, resuming the daemon logged:
  `构型1 [solver] 状态为 Completed 但输出文件缺失，重置为 Waiting`.
- The preceding SSH/SFTP message was not a confirmed missing file; it was an
  exception while probing `D:/xkz_1020/case/model_gen4_1.dat.h5`.
- A direct SFTP stat immediately afterwards confirmed both solver artifacts
  existed on `WS-A`:
  - `D:/xkz_1020/case/model_gen4_1.cas.h5`
  - `D:/xkz_1020/case/model_gen4_1.dat.h5`

Root cause:

- `PipelineScheduler._remote_files_exist()` used
  `RemoteWorkstation.get_remote_file_size()`.
- That helper returns `None` both for "missing" and for an indeterminate probe
  failure.
- The scheduler treated `None` as a confirmed missing file and reset a valid
  Completed solver step.

Code fix implemented locally:

- `_remote_files_exist()` now returns `None` for size-probe `None`, preserving
  Completed status when remote validation is indeterminate.
- Added regression:
  `tests/test_scheduler_modules.py::TestPipelineSchedulerStartRecovery::test_completed_solver_kept_when_remote_size_probe_is_unknown`.
- Focused tests passed for completed-output validation, plus ruff on touched
  scheduler/test files.

Runtime handling for active E2E:

- `config 1` solver artifacts are valid and should be restored to Completed.
- `config 1` postprocess research metric files were not present in the expected
  postprocess/metrics paths, so postprocess should remain Waiting and be rerun
  with the fixed dynamic cell-zone script.

## Finding 9: standalone postprocess reran a fragile GUI video journal even when videos already existed

Observed behavior:

- `config 1` and `config 2` standalone postprocess failed shortly after launch.
- The Fluent error was:
  `cx-name-to-id: cannot find widget: "Video Options*Table1*IntegerEntry2(FPS)"`.
- This failure happened in the recorded video-export journal before the metrics
  script could run.
- For already completed solver cases, the final animation files may already be
  present from an earlier same-session solver-to-postprocess handoff, so rerun
  recovery does not always need to execute the video GUI journal again.

Root cause:

- `batch_postprocess_gen4.py` always launched Fluent and executed
  `solver_post_gen4.jou` for standalone postprocess recovery.
- The video-export journal depends on GUI widget paths that are not stable in
  every standalone Fluent GUI session.
- This made recovery fail before the robust PyFluent metrics path could run.

Code fix implemented locally:

- `batch_postprocess_gen4.py` now checks for both final animation outputs before
  launching the video journal:
  - `v_gen4_<config>.mp4`
  - `t_gen4_<config>.mp4`
- If both files already exist, standalone recovery skips the video journals,
  runs the metrics postprocess, and writes the postprocess completion flag.
- If the journal fails with the known `Video Options` widget error but the final
  videos are present by then, the script logs a warning and continues to metrics
  instead of failing the whole postprocess step.
- Added focused tests in `tests/test_batch_postprocess_script.py` for both
  paths. Focused test and lint commands passed:
  - `.venv\Scripts\python.exe -m pytest tests/test_batch_postprocess_script.py -q`
  - `.venv\Scripts\python.exe -m ruff check executor/remote_scripts/batch_postprocess_gen4.py tests/test_batch_postprocess_script.py`

Runtime handling for active E2E:

- The fixed script has been synced to
  `/root/AutoFluidSimulation/executor/remote_scripts/batch_postprocess_gen4.py`.
- Configs 1 and 2 should be retried via client/IPC `reset_step` and
  `clean_step` for `postprocess`, preferably when their assigned workstations
  are idle so standalone postprocess does not compete with active solver work.

Follow-up finding during config 2 retry:

- After `config 7` completed on `WS-B`, `config 2` standalone postprocess was
  retried on `WS-B` at `2026-06-27 02:05:43`.
- `config 2` still did not have final animation files, so the retry correctly
  attempted the video journal instead of the "skip existing videos" path.
- It failed again at the same GUI control:
  `Video Options*Table1*IntegerEntry2(FPS)`.
- The PyFluent exception text surfaced only as `RuntimeError: error:
  wta[1](string)`, so checking only the exception string is insufficient for
  detecting this journal failure.

Second code fix implemented locally:

- Removed the `Video Options` GUI dialog block from
  `executor/remote_scripts/solver_post_gen4.jou`.
- The journal still selects MP4 output and applies both recorded animation
  sequences, but no longer sets FPS/resolution through unstable widget paths.
- Static verification passed: no `Video Options`, `FPS`, or
  `Select Resolution` references remain in the journal.
- Synced the updated journal to
  `/root/AutoFluidSimulation/executor/remote_scripts/solver_post_gen4.jou`.
- Runtime verification:
  - Retried `config 2` postprocess after `pause`, `reset_step`,
    `clean_step`, and `start`.
  - The workstation sync uploaded the updated `solver_post_gen4.jou`.
  - `config 2` standalone postprocess completed at about
    `2026-06-27 02:36`, with dashboard showing `postprocess=Completed`.

## Finding 10: WS-B idles after finishing its own Meshing-derived configs

Observed behavior:

- After `config 2` was recovered and completed on `WS-B`, the dashboard showed
  `WS-B` healthy and idle, while `config 5` was running on `WS-C`,
  `config 6` was running on `WS-A`, and `config 8/9/10` were still waiting.
- Current assignments at about `2026-06-27 03:11` were:
  - `config 2`, `config 7` -> `WS-B`, both fully Completed.
  - `config 8` -> `WS-C`, Solver Waiting.
  - `config 9`, `config 10` -> `WS-A`, Solver Waiting.
- `engine.scheduler.barrier.log` shows the `WS-B` dispatcher exiting after
  completing `config 2` postprocess at about `2026-06-27 02:35:41`.
- The earlier `config 2` postprocess error did not prevent `WS-B` from starting
  its own next Solver task:
  - `config 2` postprocess -> Error at `2026-06-26 22:44:59`.
  - `config 7` solver -> Running immediately at `2026-06-26 22:44:59`.
  - `config 7` solver completed at `2026-06-27 01:50:43`, then same-session
    postprocess completed at `2026-06-27 02:03:21`.

Conclusion:

- Dynamic workstation claim currently exists only for the Transfer/Meshing
  phase. `WorkstationSlotCoordinator.claim()` writes the selected workstation
  to `steps.workstation_id` before upload/meshing.
- Solver dispatch is workstation-scoped. `BarrierCoordinator._next_solver_config()`
  filters candidates by `_workstation_for_config(cn) in allowed_workstations`
  for Running recovery, PostProcess backlog, and new Solver work.
- Therefore a `WS-B` solver dispatcher cannot select `config 8/9/10` once those
  configs are already bound to `WS-C` or `WS-A`.
- This is not a current WS-B reconnect failure: dashboard health reported
  `WS-B=ok`, and the `remote_tasks` table had no active WS-B task.
- This matches the intended design constraint: each workstation must continue
  Solver/PostProcess only for the Meshing artifacts it produced locally.

Design constraint confirmed:

- Solver reads `model_gen4_<config>.msh.h5` from the selected workstation's
  configured `msh_dir`.
- Meshing outputs are workstation-local under the same path string
  (`D:\xkz_1020\msh`) on different machines.
- The program should not migrate Meshing artifacts or let an idle workstation
  steal Solver work generated elsewhere.
- User clarified this is intentional target behavior, not a limitation to be
  worked around: each workstation must run Solver/PostProcess only for the
  Meshing artifacts it produced locally.

Possible improvement:

- Improve dashboard/TUI wording so an idle workstation with no local queued
  Solver/PostProcess work is visibly distinguished from a stuck or disconnected
  workstation.
- Add a regression test documenting that an idle ready workstation must not
  pick Solver work whose Meshing output belongs to another workstation.
  Implemented locally as
  `tests/test_scheduler_modules.py::TestBarrierCoordinator::test_idle_workstation_does_not_steal_solver_from_other_workstations`.

Runtime handling for active E2E:

- Do not manually reassign `config 8/9/10` to `WS-B` in the current DB.
- Let `config 5/8` continue on `WS-C` and `config 6/9/10` continue on `WS-A`
  according to their Meshing-derived workstation ownership.

## Finding 11: Solver remote failure was reported as generic timeout

Observed behavior:

- `config 3` was marked `solver=Error` with message `求解超时` at about
  `2026-06-27 03:00:10`.
- The daemon-side solver timeout is `28800s`, and `config 3` started at about
  `2026-06-26 22:39:26`, so this was not a natural scheduler timeout.
- WS-C task log `D:/xkz_1020/flags/autofluid_bg_323109d5a685.log` shows the
  real failure near iteration 420:
  `BAD TERMINATION OF ONE OF YOUR APPLICATION PROCESSES`, followed by
  `Fatal error identified on the Fluent server: fatal error: disconnected / ended abnormally`.
- No `model_gen4_3.cas.h5` or `model_gen4_3.dat.h5` existed after failure; the
  mesh file `model_gen4_3.msh.h5` still existed on WS-C.

Root cause:

- `RemoteExecutor.wait_solver_completion()` detected
  `solver_done_3.txt.error` and returned `False`, but unlike Meshing it did
  not preserve the remote error flag/log summary in a `last_solver_error`
  field.
- `BarrierCoordinator._wait_for_solver_completion()` mapped every non-paused,
  non-stopped `False` return to `求解超时`, losing the actual Fluent failure.

Code fix implemented locally:

- `RemoteExecutor` now records `last_solver_error` from the solver error flag
  and wrapper log, and also records explicit partial-output and timeout errors.
- `TaskRunner.wait_solver_completion()` propagates that field.
- `BarrierCoordinator` writes the propagated message when Solver waiting fails,
  falling back to `求解超时` only when no specific error is available.
- Focused tests passed:
  - `.venv\Scripts\python.exe -m pytest tests/test_remote_executor.py::test_wait_solver_completion_records_remote_error_summary -q`
  - `.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py::TestBarrierCoordinator::test_solver_wait_failure_uses_runner_error_message -q`
  - Related 3-test RemoteExecutor and 3-test BarrierCoordinator subsets.
  - `ruff check` on touched Python files.

Runtime handling for active E2E:

- Do not retry `config 3` while WS-C is running `config 5`.
- After WS-C is idle, use client/IPC `pause`, `reset_step config=3 solver`,
  `clean_step config=3 solver`, then `start` so the existing WS-C mesh is reused
  and Solver/PostProcess reruns on WS-C.

## Finding 12: client commands cannot stop one stuck remote task

Observed behavior:

- `config 5` on `WS-C` became stuck during Fluent startup after a license denial.
- After `pause`, both user-level IPC operations were refused:
  - `clean_step {"config_name": 5, "step_name": "solver"}` ->
    `构型5的 solver 步骤仍在执行，清理前请先等待该步骤结束或 stop 后再操作`
  - `reset_step {"config_name": 5, "step_name": "solver"}` ->
    `构型5的 solver 步骤仍在执行，重置前请先等待该步骤结束或 stop 后再操作`
- The only user-level alternative exposed by that message is broad `stop`,
  which would affect unrelated active work on other workstations.

Root cause:

- Mutation safety correctly blocks reset/clean while a step is still Running.
- There is no narrow client command to terminate exactly one tracked remote
  task and let the scheduler converge that step to a terminal/retryable state.

Runtime handling for active E2E:

- Because the task was confirmed hung and not producing solver output, the
  `WS-C` scheduled task `AutoFluid_8977f62dd089` and its `cx2410.exe` /
  `ansyscl.exe` child were manually terminated.
- A `solver_done_5.txt.error` flag was written with the license-startup failure
  reason so the daemon consumed the failure through its normal polling path.
- After resume, `config 5` was marked Error and then reset to Waiting by the
  existing resume/recovery path; `config 8` started on `WS-C`.

Required durable fix:

- Add a client/IPC operation for a single tracked remote task, for example
  `stop_step config=<N> step=solver`, that:
  - queries the persisted `remote_tasks` row,
  - ends/deletes only that scheduled task and known child process tree,
  - writes a clear error message or transitions the step to a controlled
    retryable state,
  - leaves other workstations' active tasks untouched.

## Finding 13: WS-C Fluent startup can fail if the license service is not ready

Observed behavior:

- `config 5` started on `WS-C` at about `2026-06-27 03:00:14`.
- The wrapper log stopped after Fluent startup configuration; no Solver
  iteration table was produced.
- Process inspection showed `conda.exe`, Python, `cx2410.exe`, and
  `ansyscl.exe`, but no active solver iteration process.
- `D:/xkz_1020/workingdir/fluent-20260627-030035-22952.trn` showed:
  `Unexpected license problem; exiting.`
- The Ansys licensing client log showed `DENIED cfd_solve_level2` with FlexNet
  error `-15,10`, connection refused to `1055@LOCALHOST;1055@localhost`.
- The local license service processes (`lmgrd.exe`, `ansyslmd.exe`) appeared
  shortly after the denial, so the failed Fluent process did not recover by
  itself.

Runtime handling for active E2E:

- After the license service was running, `config 8` was started on `WS-C` and
  progressed into Solver iterations, confirming the workstation became usable
  again.

Required durable fix:

- `check` / worker readiness should verify the effective Fluent license path
  and ability to acquire required Solver features before allowing a long Solver
  task to start.
- Solver startup monitoring should fail fast when Fluent exits during launch
  and no iteration/progress output appears for a bounded startup window.

## Final E2E completion evidence

- `config 5` restarted on `WS-C` as task `AutoFluid_cce38890e721`.
- Low-frequency monitoring showed normal solver progress:
  - `2026-06-27 15:57`: `367/1000`
  - `2026-06-27 16:57`: `569/1000`
  - `2026-06-27 17:58`: `770/1000`
  - `2026-06-27 18:49`: `937/1000`
  - `2026-06-27 19:04`: `989/1000`
- At `2026-06-27 19:10`, dashboard showed:
  - `solver`: `Completed=10`
  - `postprocess`: `Completed=9`, `Running=1`
  - `config 5`: `solver=Completed`, `postprocess=Running`.
- The same remote task log
  `D:/xkz_1020/flags/autofluid_bg_cce38890e721.log` records:
  - `Solver 完成标志已写入: ...solver_done_5.txt`
  - immediately followed by
    `正在执行后处理 Journal: D:\xkz_1020\scripts/solver_post_gen4.jou`.
- Therefore `config 5` used the intended same-session solver-to-postprocess
  handoff; it did not launch a separate Fluent process to reread case/data.
- At `2026-06-27 19:21`, dashboard showed:
  - engine status `stopped`
  - `solver=Completed`: 10/10
  - `postprocess=Completed`: 10/10.
- Final DB verification after `quit full`:
  - `remote_tasks`: empty
  - `solver`: `[{"status": "Completed", "n": 10}]`
  - `postprocess`: `[{"status": "Completed", "n": 10}]`.

## Final cleanup note

- After `quit full`, no current `config 5` Fluent process remained on `WS-C`.
- WS-A and WS-B still had older Fluent/Cortex processes and disabled
  `AutoFluid_*` scheduled tasks from earlier failed/recovered attempts.
- Manual final cleanup removed:
  - WS-A: all remaining `fluent.exe` / `cx2410.exe` processes and 7
    `AutoFluid_*` scheduled tasks.
  - WS-B: all remaining `fluent.exe` / `cx2410.exe` processes and 5
    `AutoFluid_*` scheduled tasks.
  - WS-C: two stale Fluent-related firewall notification processes and 2
    old `AutoFluid_*` scheduled tasks.
- Final direct workstation verification showed no `fluent.exe`, no
  `cx2410.exe`, and no `AutoFluid_*` scheduled tasks on WS-A, WS-B, or WS-C.

Follow-up:

- Successful completed-task cleanup does delete the tracked scheduled task, as
  shown by `AutoFluid_cce38890e721` being deleted at `2026-06-27 19:17:36`.
- Historical Fluent remnants still require a task-specific detached-process
  cleanup improvement, because Fluent/Cortex/MPI descendants can survive after
  earlier interrupted or failed task wrappers are gone.

