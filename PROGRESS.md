# Live Feed Engine — Progress Log

See `live_feed_engine_plan.md` for the full phased plan. This file tracks where we actually are.

## Status: Phase 1 — in progress (load test run, bottleneck not yet diagnosed)

### Done
- Postgres running via `docker-compose.yml` (`feed_postgres`, port 5432), schema loaded from `init.sql`
  (`publisher`, `article` tables, 4 seed publishers).
- Fixed a bug in `rss_poller.py`: DSN had the wrong port (`5342` → `5432`).
- Ran `rss_poller.py` manually (one-shot `poll_once()`) — confirmed real RSS ingestion works.
  Reuters feed currently returns 0 entries (feed itself, not our code) — BBC/TechCrunch/HN work.
- Built `main.py` — FastAPI app with `GET /feed?region=&limit=&cursor=`, hand-written (not
  delegated) so the user can learn FastAPI concepts directly. Confirmed working against real data.
  - `asyncpg` connection pool via `lifespan` context manager (`app.state.pool`)
  - Pydantic `FeedItem` response model
  - Cursor-based pagination on `article.id` (`WHERE id < cursor ORDER BY id DESC`) — verified
    correct, no drift/overlap between pages
  - `DATABASE_URL` env var with localhost fallback (`os.environ.get`), ready for containerization
  - Gotcha learned: `asyncpg.Record` supports mapping access (`record["key"]`) but not attribute
    access — Pydantic validation needs `dict(r)` or `**r`, not the raw `Record`.
  - Gotcha learned: Pydantic v2 won't silently coerce `datetime → str`; model field must be typed
    `datetime` (Pydantic serializes it to ISO-8601 in the JSON response automatically).
- Set up `pyrightconfig.json` (`venvPath: "."`, `venv: ".venv"`) so nvim's Pyright resolves imports
  against `.venv` instead of the system Python.

### Environment
- Local venv at `.venv/` with `feedparser`, `psycopg2-binary`, `fastapi`, `uvicorn[standard]`,
  `pydantic`, `asyncpg` installed.
- Run API: `.venv/bin/uvicorn main:app --reload --port 8000`, docs at `/docs`.

### Phase 0 — complete
- `Dockerfile` (renamed from lowercase `dockerfile` — Compose's `build: .` shorthand needs the
  capitalized name on a case-sensitive filesystem) builds the FastAPI service: copies
  `requirements.txt` first for layer caching, installs deps, copies `main.py`, runs
  `uvicorn main:app --host 0.0.0.0 --port 8000` (no `--reload` in the container).
- `requirements.txt` added (asyncpg, fastapi, feedparser, psycopg2-binary, pydantic, uvicorn,
  pinned versions).
- `api` service added to `docker-compose.yml`: `DATABASE_URL` points at the compose service name
  `postgres` (not `localhost`), `depends_on: postgres: condition: service_healthy` so it waits for
  the existing `pg_isready` healthcheck instead of racing Postgres on first boot.
- Confirmed working: `docker compose up --build`, API reachable on port 8000.

### Phase 1 — in progress
- `seed.py` added: seeds synthetic articles via `COPY ... FROM STDIN` (not row-by-row `INSERT` —
  too slow at this volume). Seeded **1,000,000 rows** into `article`.
- `k6/load_test.js` added: `ramping-vus` scenario, 0→1000 VUs over 5 stages (30s each), random
  region per request against `GET /feed`, threshold `p(99)<200ms`.
- **First run result (1M rows, up to 1000 VUs):** threshold failed — p99 = **10.93s** (vs 200ms
  target), median 914ms, max 32.65s. **0% request failures** (all 200s) — this is queuing/latency
  degradation, not crashes. Throughput plateaued around ~205 req/s even as VUs ramped to 1000,
  meaning something upstream is capping drain rate rather than requests being rejected outright.
- Visual summary of Phase 0 + Phase 1 learnings saved to Obsidian:
  `Learning/Backend Engineering/Python/FastAPI/fastapi-phase1-learnings.html`.

### Not started yet
- Diagnose the bottleneck behind the 200ms→10.9s gap: check `asyncpg` pool `min_size`/`max_size`
  in `main.py` (default pools are small — likely first suspect at 1000 concurrent VUs) vs.
  `EXPLAIN ANALYZE` on the `/feed` query at 1M rows (real query-cost problem vs. pure queuing).
  This determines whether Phase 2 (Redis) is the right next move or a bigger pool alone recovers
  most of the latency.
- rss_poller.py is still run manually, one-shot — not yet running continuously in the background
  or containerized.

### Next session: pick up here
1. Check `asyncpg` pool size in `main.py`.
2. Run `EXPLAIN ANALYZE` on the `/feed` query against the 1M-row table.
3. Based on findings, either tune the pool/query or move to Phase 2 (Redis).
