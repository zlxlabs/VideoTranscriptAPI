过程中请使用中文和我沟通，但 console 里请优先使用英文。
# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `src/video_transcript_api`: `api/server.py` hosts FastAPI, `downloaders/` contains platform adapters, `transcriber/` wraps CapsWriter and FunASR clients, and `utils/` now splits into focused subpackages (`logging/`, `cache/`, `llm/`, `rendering/`, `notifications/`, `accounts/`, `timeutil/`, `risk_control/`). Templates remain in `src/web/templates`. Tests are separated within `tests/` by scope (unit, integration, performance, manual, llm, cache, features, platforms). Configuration examples sit in `config/*.example.json`, while live secrets stay in `config/config.json`. Runtime caches, SQLite stores, and logs go to `data/`; automation helpers live in `scripts/`. Launch the API through `main.py`.

## Build, Test, and Development Commands

### Using uv (Recommended)

```bash
# Install uv (if not already installed)
pip install uv

# Sync dependencies (auto-creates .venv)
uv sync

# Start the API when transcription backends are reachable
uv run python main.py --start

# Run unit and integration suites
uv run pytest tests/unit
uv run pytest tests/integration

# Run performance or manual tests (when services and credentials are configured)
uv run python tests/performance/test_concurrent.py
uv run python tests/manual/test_transcribe.py <audio_path>

# Run all tests (unittest-style discovery)
uv run python scripts/run_tests.py

# Add new dependencies
uv add <package-name>

# Update lockfile
uv lock
```

### Using pip (Traditional)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Start the API when transcription backends are reachable
python main.py --start

# Run unit and integration suites
python -m pytest tests/unit
python -m pytest tests/integration

# Run performance or manual tests (when services and credentials are configured)
python tests/performance/test_concurrent.py
python tests/manual/test_transcribe.py <audio_path>

# Run all tests (unittest-style discovery)
python scripts/run_tests.py
```

## Coding Style & Naming Conventions
Target Python 3.11+, keep PEP 8 spacing (4-space indents), and use snake_case for modules, functions, and variables. Follow the established Google-style docstrings on public APIs. Route logging through `video_transcript_api.utils.logging.setup_logger` so loguru manages stdout and rotation in `logs/`, and keep console output ASCII-only per `CLAUDE.md`. Prefer type hints and build on helpers inside the relevant `utils.*` subpackages to keep features modular.

## Testing Guidelines
Prefer pytest, naming files `test_*.py` for compatibility with pytest and the bundled unittest runner. Mock CapsWriter, FunASR, TikHub, and WeCom clients in fast feedback tests; reserve `tests/manual/` and `tests/performance/` for orchestrated runs. Redirect transient media into `tests/cache/` and clean up afterward to avoid polluting `data/`. Update `tests/README.md` when you add new suites or flags.

## Commit & Pull Request Guidelines
Commit messages follow the current log style: concise, imperative Chinese summaries (`修复 API 并发重试`). Group related edits before opening a PR. PR descriptions should outline scope, note config or schema touchpoints, reference issues with `#123`, and attach evidence (pytest output, manual steps, API samples). Flag any follow-up actions such as restarting services or updating `config/config.json`.

## Security & Configuration Notes
Do not commit live credentials; extend `config.example.json` and document defaults instead. Keep generated artifacts in `data/` and `logs/` out of patches unless troubleshooting. When working with remote transcription servers, load tokens from environment variables and clear `data/temp` via `scripts/cleanup_cache.py` after tests so sensitive media does not linger.
