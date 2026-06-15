# AutoFluid Agent Guide

Use this file as the repository entry point. Keep it short; load the
language-specific instruction files before changing source code.

## Copilot CLI Workers

When Codex delegates work to Copilot CLI, use the installed
`copilot-orchestrator` workflow and the user wrapper:

```powershell
& "$HOME\.copilot\codex-worker.ps1" -Repo "<repo>" -Prompt "<task>" -Agent codex-investigator
```

Available local agents:

- `codex-investigator`: read-only investigation and code-path mapping.
- `codex-implementer`: scoped edits; use only with `-AllowWrite`.
- `codex-reviewer`: read-only diff or change review.

Use `login:true` for Codex shell calls that launch Copilot so the PowerShell
profile provides the active `mimo-v2.5-pro` provider settings. Do not pass
`--effort` directly for this model.

## Environment

Use the project virtual environment for Python:

`C:\Users\XKZ\Documents\VSCode Projects\AutoFluidSimulation\.venv\Scripts\python.exe`

Common commands:

- Tests: `.venv\Scripts\python.exe -m pytest`
- Pip: `.venv\Scripts\python.exe -m pip`
- Mypy: `.venv\Scripts\python.exe -m mypy`
- Ruff: `.venv\Scripts\python.exe -m ruff`

Avoid bare `python`; it may resolve to the wrong interpreter.

## Server File Policy

Tracked source changes are local-first. Edit tracked files in this Windows
checkout, then use the normal commit-triggered server sync.

Direct server edits are only for `.gitignore`-ignored runtime or environment
files such as local `.env`, secrets, logs, caches, and deployment state.

## Instruction Routing

- Python: `.github/instructions/python.instructions.md`
- Rust: `.github/instructions/rust.instructions.md`
- C#: `.github/instructions/csharp.instructions.md`
- Review prompts:
  - `.github/prompts/review-python.prompt.md`
  - `.github/prompts/review-rust.prompt.md`
  - `.github/prompts/review-csharp.prompt.md`

Load each relevant instruction file for the files being changed. For mixed
tasks, keep edits scoped by language and do not mix idioms or syntax across
Python, Rust, and C# files.

## Architecture Snapshot

Main layers:

- Python daemon and executors: `engine/`, `executor/`, `ipc/`, `utils/`
- Rust TUI client: `autofluid-tui/`
- C# SpaceClaim bridge: `bridge/SpaceClaimBridge/`

Shared contracts:

- Python daemon exposes JSON-over-TCP IPC on port `9527`.
- Rust TUI polls IPC asynchronously with `tokio` and renders with `ratatui`.
- Python launches the SpaceClaim bridge through subprocesses.
- `autofluid_config.toml` is shared by Python config loading and the Rust TUI;
  shared fields must stay aligned and use `snake_case`.
- C# `ExitCode` changes must stay compatible with Python `SCProcessPool`.

## Quality Gates

Run the smallest relevant gate after changes:

- Python: `.venv\Scripts\python.exe -m ruff check .`,
  `.venv\Scripts\python.exe -m mypy .`,
  `.venv\Scripts\python.exe -m pytest tests/`
- Rust, from `autofluid-tui/`: `cargo check`,
  `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test`
- C#, from `bridge/SpaceClaimBridge/`: `compile.bat` or `compile_noref.bat`

If a gate cannot be run, state why and run the best narrower check available.

## Reviews

When asked to review, use the matching review prompt, report findings first,
order by severity, and include file/line references. Report cross-language
contract issues explicitly.

## Commit Messages

Use Chinese commit messages:

`<type>: <description>`

Allowed types: `feat`, `fix`, `refactor`, `perf`, `docs`, `chore`, `test`,
`style`.

Keep the description concise and no longer than 72 characters.
