# Repository Guidelines

## Project Structure & Module Organization

This repository is currently an empty scaffold: it has no application source, tests, assets, or package manifest. Keep new code in a clearly named source directory, such as `src/`, and place tests in `tests/` or alongside the code they cover. Add configuration and documentation at the repository root only when they apply to the whole project. Update this guide once the project layout is established.

## Build, Test, and Development Commands

No build, test, or local run commands are configured yet. When adding a toolchain, document its setup and exact commands in the README and expose repeatable scripts through its standard entry point (for example, `package.json` scripts for a Node.js project). Run the relevant build and test commands before opening a pull request.

## Coding Style & Naming Conventions

Follow the conventions of the language and formatter chosen for each new module. Use descriptive names, consistent indentation, and small, focused files. Configure a formatter and linter when the first source files are added, then commit their configuration so contributors can run the same checks locally. Avoid introducing a naming pattern that conflicts with existing modules as the repository grows.

## Testing Guidelines

There is no test framework or coverage target yet. Add tests with each behavior change and name them after the behavior they verify. Document the test command and any required fixtures or services when the first test suite is introduced. Keep tests deterministic and independent of personal credentials.

## Commit & Pull Request Guidelines

The repository has no commit history, so no established commit-message convention can be inferred. Use a short, imperative subject that describes the change, such as `Add message parsing tests`. Pull requests should explain the purpose, summarize the implementation, and list the checks run. Link relevant issues and include screenshots only for visible interface changes.

## Security & Configuration

Do not commit credentials, tokens, or private email content. Use environment variables for secrets and provide a safe example configuration if local setup requires them.
