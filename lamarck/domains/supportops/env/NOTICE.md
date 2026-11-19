# Provenance

The `server/` and `agent/` packages here are copied from the author's own project
**agent-eval-mcp** (`/Users/xunxun/agent-eval-mcp`, branch `rrsi-wip`, commit `3b186b3`),
with these changes only:

- `server/app.py`: the standalone `main()` entry point and its seeding import are removed.
- `agent/providers.py`: reduced to the provider protocol and error types (no OpenAI SDK in the sandbox).
- import paths adjusted to this package.

The task sets (`../tasks_*.yaml`), stores (`../stores.py`), seed data (`../seed.py`),
schema/loader (`../tasks.py`) and scorers (`../scorers.py`) come from the same commit and stay
host-side: they are never copied into the sandbox image.
