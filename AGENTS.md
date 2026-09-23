# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/gmail_agent/`, with fake and real Gmail MCP backends in `src/fake_gmail_mcp/` and `src/real_gmail_mcp/`. Tests live in `tests/`; static taxonomy and label mappings live in `config/`. Runtime scripts are in `scripts/` and systemd templates in `deploy/systemd/`. Keep project-wide configuration and documentation at the root.

## Build, Test, and Development Commands

Use Python 3.12+ and `uv sync --locked` to set up dependencies. Run the full suite with `uv run pytest -q` (or `.venv/bin/python -m pytest -q` in the existing environment). Local commands and runtime setup are documented in README.md. Run relevant checks before opening a pull request.

## Coding Style & Naming Conventions

Follow the conventions of the language and formatter chosen for each new module. Use descriptive names, consistent indentation, and small, focused files. Configure a formatter and linter when the first source files are added, then commit their configuration so contributors can run the same checks locally. Avoid introducing a naming pattern that conflicts with existing modules as the repository grows.

## Testing Guidelines

Use pytest and local fakes for Gemini and Gmail. Add deterministic tests for behavior changes, including persistence migrations and mutation safety. Tests must not depend on personal credentials or live services. No coverage target is configured.

## Commit & Pull Request Guidelines

The repository has no commit history, so no established commit-message convention can be inferred. Use a short, imperative subject that describes the change, such as `Add message parsing tests`. Pull requests should explain the purpose, summarize the implementation, and list the checks run. Link relevant issues and include screenshots only for visible interface changes.

## Security & Configuration

Do not commit credentials, tokens, or private email content. Use environment variables for secrets and provide a safe example configuration if local setup requires them.
