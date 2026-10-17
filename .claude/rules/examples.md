---
description: "Conventions for runnable example scripts"
paths:
  - "examples/**"
---

# Example Conventions

## File Structure

Each example is one script under `examples/`, named for what it shows (`basic_usage.py`):

1. Module docstring: a single line — `"""Example: <title and brief description>."""`
2. Imports: standard library, blank line, third-party, blank line, this package — by its public
   name, never from `src/`.
3. Named sections separated by a banner comment (see below).
4. Helper functions, each with a one-line docstring.
5. `main()` as the single entry point, called under `if __name__ == "__main__":`.

## Section Banners

Use a dashed banner to visually separate top-level sections:

```python
# ---------------------------------------------------------------------------
# Section name
# ---------------------------------------------------------------------------
```

Typical sections: helpers, then `# Entrypoint`.

## main() Pattern

- `def main() -> None`, fully typed: examples are linted and type-checked with the package.
- Errors: catch the package's base exception, print to `sys.stderr` and `sys.exit(1)`; never swallow
  one.
- Long-running examples handle `KeyboardInterrupt` and close what they open.

## Running Examples

```bash
uv run python examples/EXAMPLE_NAME.py
```

## README

The `examples/` directory has a `README.md` with:

1. `# Examples` heading
2. Brief intro: "Runnable examples demonstrating [package] usage."
3. `## Prerequisites` (if external services required)
4. `## Environment Variables` table (if any)
5. `## Running Examples` with `uv run python examples/...` commands
6. `## Example Files` table: `| Example | Description |`
