# Log Directory Structure Design

## Background

The current log layout mixes several concepts under the same `logs/` root:

- Session logs are grouped under `logs/daemon/<timestamp>/` and
  `logs/client/<timestamp>/`.
- Long-running service logs such as `alert_watcher.log` and
  `local_worker_autostart.log` are written directly under `logs/`.
- The server daemon bootstrap log is written as `logs/autofluid-daemon.out`
  on ocar.
- Tunnel logs are written to the operating-system temp directory.
- Remote workstation task logs are kept beside task state files under
  `flag_dir`.

This makes it hard to answer two operational questions quickly:

1. Which machine produced this log?
2. Which component or lifecycle does this log belong to?

## Goals

- Separate logs by machine scope: local Windows client, ocar server, and remote
  workstations.
- Keep session logs, service logs, tunnel logs, and exported UI logs in
  predictable locations.
- Keep runtime state files distinct from ordinary diagnostic logs.
- Preserve low-risk fallback behavior for paths that may not be writable.
- Avoid a large run-id propagation change in the first iteration.

## Non-Goals

- Do not redesign task state storage in this change.
- Do not move solver progress JSON into ordinary log storage.
- Do not require all processes to share a global run id.
- Do not change log message formatting or broadcast filtering.

## Recommended Layout

Use machine scope as the first-level grouping under the configured log root:

```text
logs/
  local/
    sessions/
      daemon/<YYYY-MM-DD_HH-MM-SS>/
      client/<YYYY-MM-DD_HH-MM-SS>/
    services/
      alert-watcher/
      local-worker/
      spaceclaim/
    tunnels/
      server-ipc/
      workstation/
      local-worker/
    exports/

  server/
    sessions/
      daemon/<YYYY-MM-DD_HH-MM-SS>/
    services/
      daemon-bootstrap/
      alert-watcher/

  workstations/
    <workstation-id>/
      tasks/
      progress/
```

`AUTOFLUID_LOG_DIR` remains the root override. The structure above is created
under that root.

## Path Mapping

| Current path | Proposed path |
| --- | --- |
| `logs/daemon/<ts>/*.log` | `logs/local/sessions/daemon/<ts>/*.log` on local, `logs/server/sessions/daemon/<ts>/*.log` on ocar |
| `logs/client/<ts>/autofluid-tui.log` | `logs/local/sessions/client/<ts>/autofluid-tui.log` |
| `logs/alert_watcher.log` | `logs/<scope>/services/alert-watcher/alert_watcher.log` |
| `logs/local_worker_autostart.log` | `logs/local/services/local-worker/local_worker_autostart.log` |
| `logs/autofluid-daemon.out` on ocar | `logs/server/services/daemon-bootstrap/autofluid-daemon.out` |
| `logs/export_YYYYMMDD_HHMMSS.log` | `logs/local/exports/export_YYYYMMDD_HHMMSS.log` |
| `%TEMP%/autofluid-server-ipc-tunnel-<port>.*.log` | `logs/local/tunnels/server-ipc/<port>.*.log`, with temp fallback |
| `%TEMP%/autofluid-<kind>-tunnel-<port>.*.log` | `logs/local/tunnels/<kind>/<port>.*.log`, with temp fallback |
| `D:/xkz_1020/flags/autofluid_bg_<hash>.log` | Keep in `flag_dir` for the first iteration; optional future `task_log_dir` |
| `D:/xkz_1020/flags/solver_progress_<config>.json` | Keep in `flag_dir`; this is task state, not a diagnostic log |

## Machine Scope

The path helper should resolve a machine scope before building paths:

- `local`: default Windows client/workstation process scope.
- `server`: daemon running with `AUTOFLUID_SERVER_MODE=server`.
- `workstations/<workstation-id>`: optional future local mirror or remote task
  log grouping.

The first implementation should use `local` and `server`. Workstation task logs
can remain in `flag_dir` until the remote task contract is intentionally
changed.

## Shared Path Helper

Add a small Python helper instead of duplicating path joins:

```text
utils/log_paths.py
  get_log_root()
  get_machine_log_scope()
  session_log_dir(process_type, timestamp)
  service_log_dir(component)
  service_log_file(component, filename)
  tunnel_log_dir(kind)
  export_log_dir()
```

Python remains the source of truth for session directories. Rust should continue
to prefer `AUTOFLUID_SESSION_LOG_DIR`, so launchers can pass the new path without
requiring Rust to duplicate every Python rule.

## Component Rules

### Session Logs

`init_session(process_type, timestamp)` should create:

```text
<log_root>/<scope>/sessions/<process_type>/<timestamp>/
```

Examples:

```text
logs/local/sessions/daemon/2026-06-18_12-30-00/
logs/local/sessions/client/2026-06-18_12-30-00/
logs/server/sessions/daemon/2026-06-18_12-30-00/
```

### Service Logs

Fixed service outputs should never go directly under `logs/`:

```text
logs/local/services/alert-watcher/alert_watcher.log
logs/local/services/local-worker/local_worker_autostart.log
logs/server/services/alert-watcher/alert_watcher.log
logs/server/services/daemon-bootstrap/autofluid-daemon.out
```

### SpaceClaim Logs

Bridge and transit logs that belong to a daemon session should stay inside that
session:

```text
logs/local/sessions/daemon/<ts>/bridge/
```

If transit runs without a daemon session, it should use:

```text
logs/local/services/spaceclaim/
```

### Tunnels

Tunnel logs should prefer repository log storage:

```text
logs/local/tunnels/server-ipc/<port>.out.log
logs/local/tunnels/server-ipc/<port>.err.log
logs/local/tunnels/workstation/<port>.supervisor.log
logs/local/tunnels/workstation/<port>.out.log
logs/local/tunnels/workstation/<port>.err.log
logs/local/tunnels/local-worker/<port>.supervisor.log
```

If the directory cannot be created or written, scripts may fall back to
`%TEMP%` to avoid blocking connectivity setup.

### TUI Exports

The TUI `export` command should write to:

```text
logs/local/exports/
```

Exports are operator-created artifacts, not session logger output.

### Remote Workstation Tasks

Keep these in `flag_dir` initially:

```text
<flag_dir>/autofluid_bg_<hash>.log
<flag_dir>/autofluid_bg_<hash>.pid
<flag_dir>/autofluid_bg_<hash>.cmd
<flag_dir>/meshing_done_<config>.txt
<flag_dir>/solver_done_<config>.txt
<flag_dir>/solver_progress_<config>.json
```

The `.log`, `.pid`, `.cmd`, `.done`, `.error`, and progress JSON files form one
remote task contract. Moving only the `.log` file would make task diagnosis less
local and risks breaking recovery code. A later change can add
`remote_config.task_log_dir` and migrate the whole contract deliberately.

## Compatibility Strategy

- Keep `AUTOFLUID_LOG_DIR` as the only root-level override.
- Keep `AUTOFLUID_SESSION_LOG_DIR` for Rust TUI file logging.
- Make Rust TUI discovery check the new `logs/local/sessions/client/<ts>/`
  location first, then the legacy `logs/client/<ts>/` location.
- Update server bootstrap commands and readiness failure messages together so
  `tail -n 80` points at the new daemon bootstrap log.
- Keep temp-file fallback for tunnel scripts.
- Do not delete or migrate old log files automatically.

## Implementation Plan

1. Add `utils/log_paths.py` with the path helper API.
2. Route Python session log creation through the helper.
3. Move fixed daemon service logs to `services/<component>/`.
4. Update SpaceClaim fallback log directory.
5. Update TUI log discovery and export path.
6. Update PowerShell tunnel scripts to prefer `logs/local/tunnels/`.
7. Update ocar daemon bootstrap command to write
   `logs/server/services/daemon-bootstrap/autofluid-daemon.out`.
8. Update tests and documentation that assert old paths.

## Verification

Run the smallest relevant gates after implementation:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_spaceclaim_bridge_source.py tests/test_sc_process_pool.py tests/test_start_autofluid_preflight.py
.venv\Scripts\python.exe -m pytest tests/test_workstation_reverse_tunnel_script.py tests/test_workstation_tunnel_watchdog.py
```

For Rust changes:

```powershell
Push-Location autofluid-tui
cargo fmt --check
cargo check
cargo test
Pop-Location
```

If Python logging internals change substantially, also run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_detail_log.py
```

## Risks

- External scripts or operator habits may still look for old root-level files.
  Mitigation: update user-facing messages and docs, and avoid deleting old logs.
- Rust TUI fallback discovery can pick a stale legacy session if ordering is
  wrong. Mitigation: prefer `AUTOFLUID_SESSION_LOG_DIR` and then new layout
  before legacy layout.
- Moving remote task logs separately from task state files could break recovery.
  Mitigation: keep workstation task files in `flag_dir` for this iteration.
- Tunnel setup should not fail just because repo logs are not writable.
  Mitigation: preserve temp fallback.
