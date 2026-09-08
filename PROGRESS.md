# Live Feed Engine — Progress Log

See `live_feed_engine_plan.md` for the full phased plan. This file tracks where we actually are.

## Status: Phase 1 — bottleneck diagnosed and largely fixed; deciding on Phase 2 (Redis) vs. PgBouncer

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

### Phase 1 — bottleneck diagnosis (in order of investigation)
1. **Pool size (`asyncpg`, `main.py`).** Found `max_size=10` — with 1000 concurrent VUs, ~990
   requests queue for a connection at any moment. Bumped to `min_size=10, max_size=50` (single
   worker at this point). Re-ran k6: p99 10.93s → **4.72–5.19s**, median 914ms → ~180ms, throughput
   ~205 → ~365-375 req/s. Real improvement, but tail latency (p99, max ~28-32s) barely moved, and
   965 iterations got interrupted (didn't finish inside k6's graceful-stop window) — a symptom that
   wasn't present in the first run, pointing at a different bottleneck layer.
2. **`EXPLAIN ANALYZE` on the actual `/feed` query** (both cursor and non-cursor variants) against
   the 1M-row table, run directly in `psql` (`docker exec -it feed_postgres psql -U feed_admin -d
   feed_engine`). Result: **Index Scan Backward on `article_pkey`** in both cases, execution time
   ~0.8–2ms. Ruled out query cost / missing index entirely — the plan the plan doc predicted
   ("expensive scans as offset pagination gets deep") does not apply here, because pagination is
   cursor-based on an indexed PK, not offset-based.
3. **`docker stats feed_api` during a k6 run** — CPU pinned at ~92–95% (of a single core) the whole
   run. Confirmed the app was CPU-bound on a single event loop (one uvicorn worker, no
   `--workers`), not I/O/query-bound. Also surfaced a second, previously-hidden failure:
   `OSError: [Errno 24] Too many open files` in the container logs — the container's file
   descriptor ulimit was too low for the connection volume.
4. **Fixes applied together:**
   - `docker-compose.yml`: added `ulimits.nofile` (`soft: 65536, hard: 65536`) to the `api`
     service.
   - `Dockerfile`: switched from bare `uvicorn` to **Gunicorn managing 4 `UvicornWorker`
     processes** (`gunicorn main:app -k uvicorn.workers.UvicornWorker --workers 4 --bind
     0.0.0.0:8000`) — spreads work across CPU cores instead of one event loop handling everything.
   - `requirements.txt`: added `gunicorn==23.0.0`.
   - `main.py`: pool tuned down to `min_size=5, max_size=20` — with 4 worker processes each owning
     an independent pool (`lifespan` runs per-worker), that's 4×20=80 total connections, kept safely
     under Postgres's `max_connections=100` (checked via `SHOW max_connections;`).
5. **Re-ran k6 after all four changes:** p99 **1.42s** (down from 10.93s → 4.72-5.19s → 1.42s),
   median **133ms**, throughput **~1114 req/s** (up from ~205 → ~365-375), **0 interrupted
   iterations** (down from 965), 0% failed checks throughout every run. Still fails the
   `p(99)<200ms` threshold, but the failure mode changed character — from "queuing catastrophically"
   to "compute-bound but stable," a ~5x-8x improvement across every dimension from one root-cause
   fix (CPU parallelism) plus the earlier pool-size fix.
- Visual summary of Phase 0 + Phase 1 learnings saved to Obsidian:
  `Learning/Backend Engineering/Python/FastAPI/fastapi-phase1-learnings.html` (created after the
  first load-test run — covers Docker/Compose wiring, k6 basics, and the initial 10.93s p99
  result; does not yet include the pool/worker/EXPLAIN ANALYZE diagnosis above).

### Not started yet
- Decide next move: push further on tuning (more workers / check Docker's own CPU allocation to
  the container) vs. treat current state as "proven Postgres+API alone has a real ceiling" and
  move to Phase 2 (Redis) — per the plan, this is exactly the trigger point for adding a cache.
- Separately flagged as worth doing regardless of the Redis decision: **PgBouncer**, since
  `max_connections=100` on Postgres will get tight fast if worker count or pool size grows further
  the way `4 workers × max_size` currently scales. Deliberately deferred so the worker/ulimit fix
  could be isolated and measured on its own first.
- rss_poller.py is still run manually, one-shot — not yet running continuously in the background
  or containerized.

### Next session: pick up here
1. Decide: tune further vs. move to Phase 2 (Redis) — current numbers already make a strong
   "here's where Postgres+API alone plateaus" case.
2. If moving to Phase 2: Redis sorted sets per region (`ZADD feed:US <timestamp> <articleId>`), API
   reads Redis first, falls back to Postgres on miss, re-run the same k6 script for a side-by-side
   before/after graph.
3. If tuning further, or in parallel: evaluate adding PgBouncer so connection count decouples from
   `workers × pool max_size`.
