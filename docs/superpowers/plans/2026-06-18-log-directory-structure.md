# Log Directory Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved machine-scoped log directory layout from `docs/log-directory-structure-design.md`.

**Architecture:** Python owns canonical log path resolution through a new `utils/log_paths.py` helper. Python launchers pass concrete session directories to Rust through `AUTOFLUID_SESSION_LOG_DIR`, while Rust keeps a compatibility fallback for legacy `logs/client/<ts>` sessions. PowerShell tunnel scripts and server bootstrap commands prefer structured repo logs while preserving temp fallbacks where startup reliability matters.

**Tech Stack:** Python 3.13, pytest, Rust `env_logger`, PowerShell scripts, existing AutoFluid config/env conventions.

---

## File Structure

- Create: `utils/log_paths.py`
  - Owns log root, machine scope, session, service, tunnel, and export path construction.
- Create: `tests/test_log_paths.py`
  - Verifies local/server path helper behavior and `AUTOFLUID_LOG_DIR` override.
- Modify: `utils/logger.py`
  - Routes `init_session()` and `build_session_log_dir()` through the helper.
- Modify: `engine/daemon.py`
  - Moves alert watcher and local-worker autostart logs into service directories.
- Modify: `engine/sc_process_pool.py`
  - Keeps session bridge logs under session `bridge/`, and uses service `spaceclaim/` fallback when no session exists.
- Modify: `executor/spaceclaim_transit.py`
  - Uses `logs/local/services/spaceclaim/` as the no-env fallback.
- Modify: `main.py`, `start_daemon.py`, `start_client.py`
  - Continue passing `AUTOFLUID_SESSION_LOG_DIR`; inherit new helper-backed paths.
- Modify: `autofluid-tui/src/lib.rs`
  - Discovers new `logs/local/sessions/client/<ts>` first, then legacy `logs/client/<ts>`.
- Modify: `autofluid-tui/src/event_handler/command.rs`
  - Writes exports under `logs/local/exports/`.
- Modify: `autofluid-tui/src/daemon_mgr.rs`
  - Points server bootstrap output and tail messages to `logs/server/services/daemon-bootstrap/autofluid-daemon.out`.
- Modify: `scripts/start_autofluid_preflight.ps1`
  - Matches Rust server bootstrap log path.
- Modify: `scripts/start_server_ipc_tunnel.ps1`, `scripts/start_workstation_reverse_tunnel.ps1`
  - Prefer structured tunnel log dirs; keep temp fallback.
- Modify: focused tests in `tests/` for the path assertions above.

---

### Task 1: Python Log Path Helper

**Files:**
- Create: `tests/test_log_paths.py`
- Create: `utils/log_paths.py`
- Modify: `utils/logger.py`

- [ ] **Step 1: Write failing path helper tests**

Create `tests/test_log_paths.py`:

```python
from __future__ import annotations

import os


def test_local_session_log_dir_uses_machine_scope(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("engine.config", fromlist=["LOCAL_PATHS"]).LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    from utils.log_paths import session_log_dir

    assert session_log_dir("daemon", "2026-06-18_12-30-00") == os.path.join(
        str(tmp_path / "logs"),
        "local",
        "sessions",
        "daemon",
        "2026-06-18_12-30-00",
    )


def test_server_session_log_dir_uses_server_scope(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("engine.config", fromlist=["LOCAL_PATHS"]).LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    from utils.log_paths import session_log_dir

    assert session_log_dir("daemon", "2026-06-18_12-30-00") == os.path.join(
        str(tmp_path / "logs"),
        "server",
        "sessions",
        "daemon",
        "2026-06-18_12-30-00",
    )


def test_service_tunnel_and_export_dirs_are_scoped(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("engine.config", fromlist=["LOCAL_PATHS"]).LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    from utils.log_paths import export_log_dir, service_log_file, tunnel_log_dir

    assert service_log_file("alert-watcher", "alert_watcher.log") == os.path.join(
        str(tmp_path / "logs"), "local", "services", "alert-watcher", "alert_watcher.log"
    )
    assert tunnel_log_dir("server-ipc") == os.path.join(
        str(tmp_path / "logs"), "local", "tunnels", "server-ipc"
    )
    assert export_log_dir() == os.path.join(str(tmp_path / "logs"), "local", "exports")
```

- [x] **Step 2: Run RED test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_log_paths.py -q
```

Expected: FAIL because `utils.log_paths` does not exist.

- [x] **Step 3: Implement `utils/log_paths.py`**

Create `utils/log_paths.py`:

```python
from __future__ import annotations

import os


def get_log_root() -> str:
    try:
        from engine.config import LOCAL_PATHS

        root = str(LOCAL_PATHS.get("log_dir") or "")
    except (ImportError, AttributeError):
        root = ""
    if root:
        return root
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def get_machine_log_scope() -> str:
    if os.environ.get("AUTOFLUID_SERVER_MODE", "").strip().lower() == "server":
        return "server"
    return "local"


def session_log_dir(process_type: str, timestamp: str) -> str:
    return os.path.join(get_log_root(), get_machine_log_scope(), "sessions", process_type, timestamp)


def service_log_dir(component: str) -> str:
    return os.path.join(get_log_root(), get_machine_log_scope(), "services", component)


def service_log_file(component: str, filename: str) -> str:
    return os.path.join(service_log_dir(component), filename)


def tunnel_log_dir(kind: str) -> str:
    return os.path.join(get_log_root(), "local", "tunnels", kind)


def export_log_dir() -> str:
    return os.path.join(get_log_root(), "local", "exports")
```

- [x] **Step 4: Route `utils/logger.py` through helper**

Update `_resolve_base_log_dir()` to call `get_log_root()`. Update `init_session()` and `build_session_log_dir()` to call `session_log_dir()`.

- [x] **Step 5: Run GREEN test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_log_paths.py -q
```

Expected: PASS.

---

### Task 2: Python Service and SpaceClaim Paths

**Files:**
- Modify: `tests/test_sc_process_pool.py`
- Modify: `tests/test_spaceclaim_bridge_source.py`
- Modify: `engine/daemon.py`
- Modify: `engine/sc_process_pool.py`
- Modify: `executor/spaceclaim_transit.py`

- [ ] **Step 1: Add failing service path source tests**

In `tests/test_spaceclaim_bridge_source.py`, add assertions that `spaceclaim_transit.py` contains `services` and `spaceclaim` fallback path terms. In a new or existing daemon source test, assert `engine/daemon.py` uses `service_log_file` for `alert_watcher.log` and `local_worker_autostart.log`.

- [ ] **Step 2: Update SC process pool unit test**

In `tests/test_sc_process_pool.py`, add a test that monkeypatches `get_session_log_dir` to return `None` and asserts `_build_bridge_log_dir()` returns `<log_dir>/local/services/spaceclaim`.

- [ ] **Step 3: Run RED tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_sc_process_pool.py tests/test_spaceclaim_bridge_source.py -q
```

Expected: FAIL on the new path assertions.

- [ ] **Step 4: Implement service path use**

Update `engine/daemon.py` to import `service_log_file` and use:

```python
log_path = service_log_file("alert-watcher", "alert_watcher.log")
log_path = service_log_file("local-worker", "local_worker_autostart.log")
```

Update `engine/sc_process_pool.py` to use `service_log_dir("spaceclaim")` when `get_session_log_dir()` is `None`.

Update `executor/spaceclaim_transit.py` fallback to `<project>/logs/local/services/spaceclaim`.

- [ ] **Step 5: Run GREEN tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_sc_process_pool.py tests/test_spaceclaim_bridge_source.py -q
```

Expected: PASS.

---

### Task 3: Rust TUI Session Discovery and Export Path

**Files:**
- Modify: `autofluid-tui/src/lib.rs`
- Modify: `autofluid-tui/src/event_handler/command.rs`

- [ ] **Step 1: Add failing Rust tests**

Add or update Rust tests so client log discovery prefers `logs/local/sessions/client/<ts>` and falls back to legacy `logs/client/<ts>`. Add a command/export test or source assertion that export path includes `logs/local/exports`.

- [ ] **Step 2: Run RED tests**

Run:

```powershell
Push-Location autofluid-tui
cargo test
Pop-Location
```

Expected: FAIL on new path expectations.

- [x] **Step 3: Implement Rust path changes**

Update `find_latest_client_session_dir()` to check:

```text
logs/local/sessions/client
logs/client
```

Update export command to write into `current_dir()/logs/local/exports`.

- [ ] **Step 4: Run GREEN tests**

Run:

```powershell
Push-Location autofluid-tui
cargo test
Pop-Location
```

Expected: PASS.

---

### Task 4: Server Bootstrap and PowerShell Tunnel Paths

**Files:**
- Modify: `tests/test_start_autofluid_preflight.py`
- Modify: `tests/test_workstation_reverse_tunnel_script.py`
- Modify: `scripts/start_autofluid_preflight.ps1`
- Modify: `scripts/start_server_ipc_tunnel.ps1`
- Modify: `scripts/start_workstation_reverse_tunnel.ps1`
- Modify: `autofluid-tui/src/daemon_mgr.rs`

- [ ] **Step 1: Update failing tests for bootstrap path**

Change `tests/test_start_autofluid_preflight.py` to expect:

```text
logs/server/services/daemon-bootstrap/autofluid-daemon.out
```

- [ ] **Step 2: Add failing tunnel path assertions**

Add tests that assert `start_server_ipc_tunnel.ps1` and `start_workstation_reverse_tunnel.ps1` include `logs/local/tunnels/server-ipc` and `logs/local/tunnels/$tunnelName`, and still contain temp fallback logic.

- [ ] **Step 3: Run RED tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_start_autofluid_preflight.py tests/test_workstation_reverse_tunnel_script.py tests/test_workstation_tunnel_watchdog.py -q
```

Expected: FAIL on new path assertions.

- [ ] **Step 4: Implement server bootstrap path**

Update PowerShell and Rust server bootstrap commands to create and write:

```text
logs/server/services/daemon-bootstrap/autofluid-daemon.out
```

Update all `tail -n 80` references in those command strings.

- [ ] **Step 5: Implement tunnel path preference**

In both tunnel scripts, build preferred log paths under repo `logs/local/tunnels/...` when possible. If creating the directory fails, use `[System.IO.Path]::GetTempPath()` fallback paths.

- [ ] **Step 6: Run GREEN tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_start_autofluid_preflight.py tests/test_workstation_reverse_tunnel_script.py tests/test_workstation_tunnel_watchdog.py -q
```

Expected: PASS.

---

### Task 5: Documentation and Final Verification

**Files:**
- Modify: `docs/log-directory-structure-design.md`
- Possibly modify: `README.md` if current public docs still state old export paths.

- [ ] **Step 1: Update design status**

Change the design doc status from proposal to implemented, or add an implementation status section listing completed path migrations and explicitly noting that remote workstation task files remain in `flag_dir`.

- [ ] **Step 2: Run targeted Python gates**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_log_paths.py tests/test_sc_process_pool.py tests/test_spaceclaim_bridge_source.py tests/test_start_autofluid_preflight.py tests/test_workstation_reverse_tunnel_script.py tests/test_workstation_tunnel_watchdog.py -q
```

Expected: PASS.

- [ ] **Step 3: Run Rust gates**

Run:

```powershell
Push-Location autofluid-tui
cargo fmt --check
cargo check
cargo test
Pop-Location
```

Expected: PASS.

- [ ] **Step 4: Run diff hygiene**

Run:

```powershell
git diff --check
```

Expected: no output and exit code 0.
