# AutoFluid Agent Guide

This file adapts the existing Copilot instructions in `.github/` for Codex.
Before editing code, use this file as the entry point and load only the
language-specific instruction file needed for the files being changed.

## Environment

Use the project virtual environment for all Python commands:

`C:\Users\XKZ\Documents\VSCode Projects\AutoFluidSimulation\.venv\Scripts\python.exe`

Do not use bare `python`, because the Codex process may resolve it to another
interpreter such as MSYS2 Python.

Command forms:

- Tests: `.venv\Scripts\python.exe -m pytest`
- Pip: `.venv\Scripts\python.exe -m pip`
- Mypy: `.venv\Scripts\python.exe -m mypy`
- Ruff: `.venv\Scripts\python.exe -m ruff`

## Source Instructions

- General implementation and architecture:
  `.github/instructions/implementation-planning.instructions.md`
- Python rules: `.github/instructions/python.instructions.md`
- Rust rules: `.github/instructions/rust.instructions.md`
- C# rules: `.github/instructions/csharp.instructions.md`
- Commit message rules: `.github/copilot-instructions.md`
- Review prompts:
  `.github/prompts/review-python.prompt.md`,
  `.github/prompts/review-rust.prompt.md`,
  `.github/prompts/review-csharp.prompt.md`

## Language Routing

- For `*.py`, read and follow `.github/instructions/python.instructions.md`.
- For `autofluid-tui/**/*.rs` and Rust TOML work, read and follow
  `.github/instructions/rust.instructions.md`.
- For `bridge/**/*.cs` and `bridge/**/*.csproj`, read and follow
  `.github/instructions/csharp.instructions.md`.
- For `.toml`, `.ini`, `.bat`, and project config files, choose the instruction
  file for the language or subsystem that owns the file.
- If a task spans multiple languages, read each relevant instruction file, but
  keep edits language-scoped and avoid mixing one language's idioms into another.

## Cross-Language Guardrails

- Never insert Python snippets into Rust or C# files.
- Never insert Rust snippets into Python or C# files.
- Never insert C# snippets into Python or Rust files.
- Do not copy external API examples or documentation blocks into source files.
- If context from another language appears while editing one language, ignore it
  unless the task explicitly concerns a cross-language interface.

## Architecture Snapshot

AutoFluid has three main layers:

- Python daemon and executors: `engine/`, `executor/`, `ipc/`, `utils/`
- Rust TUI client: `autofluid-tui/`
- C# SpaceClaim bridge: `bridge/SpaceClaimBridge/`

Key interactions:

- Python daemon exposes JSON-over-TCP IPC on port `9527`.
- Rust TUI polls IPC asynchronously with `tokio` and renders with `ratatui`.
- Python launches SpaceClaim Bridge through subprocesses.
- `autofluid_config.toml` is shared between Python config loading and the Rust
  TUI settings page; shared field names must remain identical `snake_case`.

High-risk compatibility areas:

- IPC protocol changes must keep Rust and Python clients compatible.
- State database schema changes must consider backward compatibility.
- C# `ExitCode` changes must be reflected in Python `SCProcessPool` handling.

## Python Rules

- Follow Python 3.10+ typing style such as `dict[str, list[int]]`.
- Public methods need complete parameter and return annotations.
- Prefer `from __future__ import annotations` in new Python modules.
- Step methods follow `execute_{step}_step()` where applicable.
- Use module log prefixes such as `[SW]`, `[SC]`, `[Transfer]`, `[Meshing]`,
  `[Solver]`, `[Scheduler]`, `[IPC]`, and `[State]`.
- Avoid `dataclasses.asdict()` for objects containing non-serializable fields
  such as `subprocess.Popen`; manually build dictionaries instead.

Quality gate after Python changes:

- `.venv\Scripts\python.exe -m ruff check .`
- `.venv\Scripts\python.exe -m mypy .`
- `.venv\Scripts\python.exe -m pytest tests/`

## Rust Rules

- Rust code lives under `autofluid-tui/`.
- Use `tokio` for async work and avoid blocking calls such as
  `std::thread::sleep` in async contexts.
- Propagate errors with `Result<T, E>`; avoid `unwrap()` in IPC and UI loops.
- Keep configuration fields `snake_case` and aligned with Python/TOML names.

Quality gate after Rust changes, from `autofluid-tui/`:

- `cargo check`
- `cargo clippy -- -D warnings`
- `cargo fmt --check`
- `cargo test`

## C# Rules

- C# bridge code lives under `bridge/SpaceClaimBridge/`.
- Target `.NET Framework 4.8`.
- Use block-scoped namespaces, `PascalCase` types/methods, `_camelCase` private
  fields, and XML `///` comments for public types and methods.
- Top-level execution should catch unhandled exceptions and return an
  appropriate `ExitCode`.
- Keep command-line arguments compatible with Python:
  `--script <path> --config <id> --stepdir <dir> --scdocdir <dir> [--timeout <sec>]`.

Quality gate after C# changes:

- Run `compile.bat` or `compile_noref.bat` in `bridge/SpaceClaimBridge/`.
- Manually verify Python-side `SCProcessPool` handles all `ExitCode` values.

## Review Mode

When asked to review:

- Use the matching `.github/prompts/review-*.prompt.md` file.
- Review only the requested language unless the user asks for cross-language
  review.
- Lead with findings ordered by severity, with file and line references.
- If an interface issue crosses language boundaries, report it as a
  cross-language interface issue before modifying other languages.

## Commit Messages

When creating commits, write Chinese commit messages in this format:

`<type>: <description>`

Allowed types:

- `feat`
- `fix`
- `refactor`
- `perf`
- `docs`
- `chore`
- `test`
- `style`

The description should be concise, in Chinese, and no longer than 72 characters.
