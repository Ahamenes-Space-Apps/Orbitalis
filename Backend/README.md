# Orbitalis_backend

Python backend for ORBITALIS, a tool for exploring NASA SPHEREx observations through time-lapse visualization.

**Status:** complete.

## Layout

```text
src/orbitalis_core/   pure science library (numpy, astropy, scipy only)
src/orbitalis/        application layer (api, gateway, pipeline, jobs, research, assistant, store, overlays, demo)
config/               presets, releases, stack policy, assistant knowledge
demo_bundle/          pre-baked offline demo data
scripts/              bake_demo.py, bench_core.py, check_boundaries.sh
archive/              one-off diagnostics and coverage exports
```

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
cp .env.example .env
uv run uvicorn orbitalis.main:app --reload --port 8080
```

## Everyday commands

| Task | Command |
|---|---|
| Lint / format | `uv run ruff check .` / `uv run ruff format .` |
| Check boundaries | `uv run lint-imports` |
| Add runtime dependency | `uv add <pkg>` |
| Add dev dependency | `uv add --dev <pkg>` |

## Notes

- The database is a disposable cache; deleting it is safe.
- `ORB_OFFLINE=1` serves the demo bundle without calling IRSA.
- The assistant is optional and capability-flagged.
