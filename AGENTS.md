过程中请使用中文和我沟通，但 console 里请优先使用英文。
# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `src/video_transcript_api`: `api/server.py` hosts FastAPI, `downloaders/` contains platform adapters, and `transcriber/` wraps CapsWriter and FunASR clients. `cache/`, `llm/`, and `risk_control/` sit next to `utils/`. `utils/` holds `logging/`, `rendering/`, `notifications/`, `accounts/`, `timeutil/`, and `ytdlp/`. Templates remain in `src/web/templates`. Tests are separated within `tests/` by scope (unit, integration, cache, manual, llm, features, platforms, and others). Concurrent load lives at `scripts/perf/concurrent_load.py`. Configuration examples sit in `config/config.example.jsonc` and `config/users.example.json`. Live secrets stay in `config/config.jsonc`, an untracked runtime file (see `.gitignore`) copied from `config/config.example.jsonc` at deploy time. Runtime caches, SQLite stores, and logs go to `data/`; automation helpers live in `scripts/`. Launch the API through `main.py`.

## Build, Test, and Development Commands

### Using uv (Recommended)

```bash
# Install uv (if not already installed)
pip install uv

# Sync dependencies (auto-creates .venv)
uv sync

# Start the API when transcription backends are reachable
uv run python main.py --start

# Run unit and integration suites (`dev` dependency group is installed by `uv sync`)
uv run pytest tests/unit
uv run pytest tests/integration

# Local/gate entry: `pytest -q tests`, skipping `tests/manual`.
# Scope source of truth: tests/README.md
make test

# Repaired pytest thin wrapper for the same discovery. Prefer `make test`.
uv run python scripts/run_tests.py

# Manual concurrent load against a local API (optional extra: perf)
uv run --extra perf python scripts/perf/concurrent_load.py

# Manual transcription of one local media file
uv run python tests/manual/test_transcribe.py <audio_path>

# Add new dependencies
uv add <package-name>

# Update lockfile
uv lock
```

### Using pip (Traditional)

Install dependencies with `uv sync` from `pyproject.toml` (the `dev` dependency
group lands in `.venv`). Then use the commands in the uv section above.
`make test` is the local gate (`pytest -q tests`, skipping `tests/manual`).

## Coding Style & Naming Conventions
Target Python 3.11+, keep PEP 8 spacing (4-space indents), and use snake_case for modules, functions, and variables. Follow the established Google-style docstrings on public APIs. Route logging through `video_transcript_api.utils.logging.setup_logger` so loguru manages stdout and rotation in `logs/`, and keep console output ASCII-only per `CLAUDE.md`. Prefer type hints and build on helpers inside the relevant `utils.*` subpackages to keep features modular.

## Testing Guidelines
Prefer pytest, naming files `test_*.py`. Mock CapsWriter, FunASR, TikHub, and WeCom clients in fast feedback tests; reserve `tests/manual/` and `scripts/perf/concurrent_load.py` for orchestrated runs. Redirect transient media into `tests/cache/` and clean up afterward to avoid polluting `data/`. Update `tests/README.md` when you add new suites or flags.

## Commit & Pull Request Guidelines
Commit messages follow the current log style: concise, imperative Chinese summaries (`修复 API 并发重试`). Group related edits before opening a PR. PR descriptions should outline scope, note config or schema touchpoints, reference issues with `#123`, and attach evidence (pytest output, manual steps, API samples). Flag any follow-up actions such as restarting services or updating `config/config.jsonc` (untracked runtime config; see `.gitignore`).

## Security & Configuration Notes
Do not commit live credentials; extend `config/config.example.jsonc` and document defaults instead. Keep generated artifacts in `data/` and `logs/` out of patches unless troubleshooting. When working with remote transcription servers, load tokens from environment variables and delete leftover media under `data/temp` after tests so sensitive media does not linger.
