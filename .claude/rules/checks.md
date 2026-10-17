---
description:
  "Exact pre-commit commands for the python-lib-micropython CI archetype, kept in sync with ci.yaml"
---

# Pre-commit Checks

This repo's `ci.yaml` is generated from `dx/ci-templates/python-lib-micropython.yaml` via
`dx ci sync` (check for drift with `dx ci drift`). Run these before committing so CI passes on the
first try:

```bash
uv run --locked ruff check            # `uv run --locked ruff check --fix` fixes what it can
uv run --locked ruff format --check   # `uv run --locked ruff format` rewrites the files
uv run --no-project --with pyyaml env -u VIRTUAL_ENV python .claude/checks/ci-jobs.py --skip-ci-only lint typecheck coverage micropython
```

`--locked` fails when `uv.lock` no longer matches `pyproject.toml`, as CI's `uv sync --locked` does:
run `uv lock` after changing a dependency and commit both files.

The `ci-jobs.py` step runs CI's jobs locally, as CI writes them:

- **Lint** — ruff, then `package.json` against `src/` and its version, then the imports of every
  module the manifest ships against what a board has.
- **Type Check** — the board stubs against the MicroPython pin, then pyright.
- **Coverage** — the tests on CPython, with the same 80% line gate, minus the Codecov upload.
- **Test (MicroPython Unix port)** — builds the pinned Unix port and `mpy-cross` once per machine
  into `~/.cache/qualithm/`, checks both match the pin, runs `tests/` on the Unix port and compiles
  every shipped module.

The first run needs `git`, `make` and a C compiler for the toolchain build; later runs reuse it.

After adding a module or bumping the version, rewrite the manifest and commit it with the change.
`package.json` and `pyproject.toml` carry one version, and the release tag refuses a mismatch:

```bash
python3 .github/micropython-package manifest
```

On PRs into `main`, CI also tests the oldest supported Python on the built wheel:
`uv sync --locked --no-editable --python 3.11 && uv run --no-sync pytest`.
