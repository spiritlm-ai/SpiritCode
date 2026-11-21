# AGENTS.md

Guidance for AI coding agents working on SpiritCode itself. Keep it in sync with the code.

## Project

SpiritCode is a terminal coding agent for the SpiritLM service (Python 3.12+, `src/spiritcode/`).
Entry points: `sprt` and `spiritcode`. All user-facing strings (notices, help, errors) are
Russian; identifiers, commit messages, and code comments are English.

## Commands

- `uv sync` — install dependencies. Use `uv`, not `pip`; `uv.lock` is committed.
- `uv run pytest` — run tests. All model/API calls must be mocked (`httpx.MockTransport`,
  `AsyncMock`); never run real paid generations.
- `uv run ruff check .` and `uv run ruff format .` — lint and format (line length 100, py312).
  CI verifies formatting strictly.
- `uv run mypy src` — strict type check.
- `uv build` + `uv run twine check "dist/*"` — package build; `scripts/check_install.py`
  installs the wheel and sdist to verify entry points.

## Architecture

Import direction (keep it acyclic): `constants` / `types` / `sanitize` / `prompts` → `ui` →
`tools` → `core` → `commands` → `cli`.

- `constants.py` — every developer-tunable value: URLs, API paths, colors, timeouts, limits.
- `types.py` — shared `Literal` types (`PermissionMode`, `NoticeKind`, `PERMISSION_MODES`).
- `sanitize.py` — `clean()` plus the ANSI/control/API-key regexes. All model output, tool
  results, and persisted data go through it.
- `prompts.py` — the system prompt (`SYSTEM`) and the compaction summary prompt (`COMPACTION`).
- `ui/` — terminal layer: `interface.py` (UI class, completer, key bindings), `keyboard.py`
  (CSI-u parser), `commands.py` (slash palette and help text), `display.py` (account rendering).
- `tools/` — agent tools: `schemas.py` (pydantic argument models, `definitions()`, `TITLES`),
  `files.py` (project discovery, reading, diffs), `executor.py` (the `Tools` class: dispatch,
  permission checks, undo snapshots, child processes).
- `core/` — agent core: `api.py` (HTTP/SSE client), `models.py` (API payload models),
  `agent.py` (agent loop), `config.py` (persisted `Settings`), `context.py` (context assembly,
  token budget), `permissions.py`, `store.py` (SQLite sessions), `credentials.py` (keyring).
- `commands/` — slash-command handlers: `chat.py` (`Chat` state), `model.py` (catalog and
  switching), `sessions.py` (new/resume/list), `account.py`, `diagnostics.py` (`/context`),
  `files.py` (`/undo`, `/diff`).
- `cli.py` — argparse entry point, thin dispatcher, and the interactive input loop.

## Code style

- Formatting is enforced by `ruff format`; two blank lines between top-level definitions and
  blank lines between logical blocks inside functions.
- mypy strict: annotate everything; `Any` is allowed only at the JSON/SSE boundary.
- Comments only for non-obvious invariants (wire-protocol quirks, safety reasons, terminal
  behavior). Never restate what the code obviously does.
- Keep functions small. Move reusable helpers and per-call constant dicts to module level.
- Dependencies live in `pyproject.toml`. `requirements.txt` is a flattened copy for pip users —
  update both together.

## Safety invariants (never break)

- API keys must never reach messages, snapshots, logs, or the terminal; the `KEY` regex scrubs
  them before persistence and display.
- Permission prompts are user control, not a sandbox: a confirmed command runs with user
  privileges.
- No automatic retries of paid generations; failed streams and compactions keep the original
  history.
- Interrupted tool calls are closed with a result, never replayed.
- `edit_file` and `edit_files` require a prior `read_file` of the unchanged content. A batch
  is a separate undo group. `/undo` restores files only if they have not changed since the edit.
- Scoped `AGENTS.md` instructions must reach the model in a request before a file tool can
  write under that scope. Only the root instructions are injected into the system context.
- Child processes receive the environment without `SPIRITLM_API_KEY`.
- Secrets, binaries, `.git`, and dependencies are excluded from automatic context collection.

## Conventions

- Tests import module paths directly (`spiritcode.core.config`, `spiritcode.ui.interface`,
  ...). When moving modules, update imports and string `patch(...)` targets in the same change.
- Never edit `uv.lock` by hand; use `uv add` / `uv remove`.
