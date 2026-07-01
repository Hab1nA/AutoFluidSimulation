# Tunnel Watchdog Architecture

This document records the two supported workstation reverse-tunnel ownership
models. It is intentionally operational: use it to decide which cleanup path is
safe during daemon restart, worker restart, and full maintenance stops.

## Ocar-Owned Supervisor

- Script: `scripts/start_workstation_reverse_tunnel.ps1`
- Owner: the server/client process that launched the monitor on ocar.
- Evidence: local PID files under `data/`, supervisor logs under
  `logs/local/tunnels/`, and the owner PID or owner marker passed to the script.
- Use case: legacy ocar-side reverse tunnel supervision where the server owns
  the relay process lifecycle.
- Cleanup: call `cleanup_tunnel_watchdog_tasks(..., include_workstation=True)`
  only for explicit full-stop or maintenance cleanup. Do not run this during
  daemon service restart or ordinary `worker restart`, because those paths must
  preserve running workstation solvers and workstation-owned tunnels.

## Workstation-Owned Tunnel

- Script: `scripts/start_workstation_owned_reverse_tunnel.ps1`
- Owner: the workstation itself. The monitor is protected by a workstation-local
  mutex and survives ocar daemon restarts.
- Evidence: workstation-side scheduled task/run-key state, workstation-side
  supervisor logs, and remote endpoint reachability from ocar.
- Use case: preferred durable tunnel mode for long-running workstation solvers.
- Cleanup: use `tools/workstation_tunnel.py` repair/status/uninstall flows. Do
  not use local worker PID cleanup or default daemon shutdown cleanup to stop
  this tunnel.

## Restart Semantics

- Daemon service restart preserves pipeline state and `remote_tasks`; it must not
  cancel remote scheduled tasks or uninstall workstation tunnels.
- `worker restart` refreshes SSH readiness and worker registry state, but keeps
  `remote_tasks` intact.
- Explicit `stop`, `quit full`, and maintenance cleanup are the only paths that
  may cancel tracked remote tasks and remove workstation legacy relay monitors.

## Health Semantics

- `ok`: a recent active SSH/system check succeeded.
- `stale`: the last active check succeeded but is older than the dashboard
  freshness window.
- `unknown`: no active evidence exists yet.
- `disconnected` or `error:*`: an active check failed.

The daemon may run low-frequency active probes to refresh stale SSH evidence,
but dashboard reads remain low-cost snapshots and must not promote cached
Paramiko transports to `ok`.
