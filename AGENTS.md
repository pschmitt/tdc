# Agent notes

`tdc` is a Python command-line client for listing and managing Todoist tasks,
projects, and sections.

- Match the CLI behavior and option conventions documented in `README.md`.
- Run `uv run ruff check .` for linting. The repository has no dedicated test
  suite.
- CI uses the shared
  [ci-workflows](https://github.com/pschmitt/ci-workflows) Python Ruff action.
- Before changing CI, check for a reusable workflow or action there and
  preserve the caller's existing behavior and check coverage.
- Do not run commands against a user's Todoist account while validating
  changes; use a test account if API behavior must be checked.
