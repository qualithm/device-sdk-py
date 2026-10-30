# Python and MicroPython Code Guidelines

The shared core runs on MicroPython and CPython. The checks enforce style, types and the subset;
this file covers what they can't.

## Layout

- `src/<package>/` holds the code. `__init__.py` re-exports the public API and sets `__all__`;
  private modules start with `_`. Ship `py.typed`.
- The shared core is every module `package.json` (mip's manifest) ships: all of `src/` except the
  host-only globs in its `exclude`. Never edit its `urls` by hand:
  `python3 .github/micropython-package manifest` rewrites them from `src/` and `version`.
- `pyproject.toml` and `package.json` carry one version; bump both, then rewrite the manifest. No
  version constant in code: a board has no `importlib.metadata`.
- `tests/` runs on both runtimes; `tests_cpython/` holds tests of host-only modules, which only
  pytest runs. Host fakes of injected dependencies live in `tests/fakes/`.

## The MicroPython subset

- The core imports only what a board has: the board stubs' modules and MicroPython's core (`array`,
  `asyncio`, `binascii`, `collections`, `io`, `json`, `os`, `re`, `struct`, `sys`, `time`). No
  `dataclasses`, `typing`, `enum`, `abc`, `logging` or `collections.abc` at run time.
- Type-only imports, `Protocol`s included, go under `if TYPE_CHECKING:` with `TYPE_CHECKING = False`
  defined in the module; annotate with strings for names imported there.
- No `**` in a dict display and no counted repetition (`{m,n}`) in a regex: MicroPython lacks both.
  Plain classes stand in for dataclasses.
- Inject what differs by board (network, crypto, storage, the clock) as constructor or function
  arguments, never created at import.

## Types

Annotate every public name. No `Any`, and no `# type: ignore` without a code and a reason. Options
after the first few arguments are keyword-only.

## Errors, runtime and docstrings

- One base exception with a subclass per failure; lowercase messages, `from err` when wrapping.
  Never catch bare `Exception` or return `None` to signal failure.
- No `print` in library code and no import-time side effects.
- Every public name has a one-line summary, then Google-style `Args:`, `Returns:` and `Raises:`.

## Tests

`unittest.TestCase` with `self.assert*` in `tests/`, so pytest and MicroPython's `unittest` both run
it: no fixtures, `parametrize` or `unittest.mock`; inject fakes. Load shared fixtures with `json`
from a path relative to the repo root. No network in unit tests.

## When code changes

A behavior change carries tests covering its new lines (the 80% gate measures `src/` on CPython).
Add dependencies with `uv add` so `uv.lock` changes with `pyproject.toml`, and justify each runtime
dependency: the core takes none a board can't install.

## Generated files

`.github/workflows/ci.yaml` and `.github/micropython-package` are generated from `dx/ci-templates/`;
change the template and run `dx ci sync` from dx, never edit them here. `CI Required` and `PR Title`
are required.
