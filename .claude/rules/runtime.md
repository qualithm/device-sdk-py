---
description: "Runtime conventions for errors, logs, settings and CLIs"
paths:
  - "**/*.py"
---

# Runtime Conventions

One set of rules for errors, logs, settings and CLIs holds in every language, so logs and errors
from different repos read and search together. This is how they look here.

## Errors

- One base exception per package (`<Component>Error`) with a subclass per failure. Messages are
  lowercase, with no trailing punctuation, and name what failed (`claim code expired`).
- Never return `None` to signal failure.

## Logging

- The shared core doesn't print or log; a board has no `logging`. Host-only modules log through
  `logging`: a `snake_case` event plus fields, never a sentence with values interpolated.
- Field keys are `snake_case` in every language, so one query reads every service. The shared names
  are `team_id`, `device_id`, `request_id`, `trace_id`, `duration_ms`, and `error` for the error,
  never `e` or `err`.

## Settings

- Settings are a `<Component>Config` class with production defaults.
- Units go in names: `_ms` for durations and `_bytes` for sizes (`timeout_ms`, `buffer_bytes`).
- Env vars it reads for its own settings start with `QUALITHM_`, because it runs on a builder's
  machine or device. Standard names (`PORT`, `LOG_LEVEL`) stay unprefixed, and credentials keep
  their own naming rule.
