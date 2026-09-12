# Live Feed Engine — Progress Log

See `live_feed_engine_plan.md` for the full phased plan. This file tracks where we actually are.

## Status: Phase 1 — diagnosis complete. CPU-bound ceiling confirmed on 4 physical cores; code-level
tuning has run out of outsized wins. Next real lever (more hardware / horizontal scaling) not yet
testable locally. Redis (Phase 2) deprioritized by reasoning, not testing.

## Collaboration mode (read this first)
Starting this session, this project is explicit **learning-mode**: the user writes all code by
hand, the assistant does not provide code unless explicitly asked to "write it down" in the
moment. When the user is stuck: they produce their own "confusion list" (Confusion Compass
technique) before the assistant adds Socratic questions; before writing new code, the user does a
rough "think on paper" pass (keywords → guessed connections → messy → reorganize, per the
make-it-wrong/make-it-shorter/make-it-again loop) rather than jumping straight to the file. Doc/
resource pointers are given only after real struggle, never code. This is a durable, cross-session
preference stored in the assistant's memory system, not just for today.

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

### Phase 1 — post-worker-fix: reasoned about Redis vs. PgBouncer vs. further tuning, then profiled
- **Reasoning pass (before touching anything):** worked through why PgBouncer (connection pooling)
  and Redis (caching) solve different problems — PgBouncer addresses connection exhaustion,
  Redis addresses expensive-to-fetch data. Since the CPU-parallelism fix already ruled out
  connection queuing as the live bottleneck (p99 1.42s, not connection-starved), the open question
  became "what's actually consuming CPU now, since `EXPLAIN ANALYZE` already showed the query
  itself is ~2ms."
- **`top` inside the `feed_api` container during a k6 run:** all 4 Gunicorn workers (PIDs 7,8,9,10)
  evenly loaded (35–80% CPU each) — ruled out the earlier "only 2 workers used" read from a stale
  `docker stats` snapshot. Critically, **`%Cpu(s)` idle = 0.0% and wait = 0.0%** across every
  snapshot, with load average climbing 3.32 → 8.41 against `nproc` = 4 cores — confirmed CPU-bound
  (not I/O-bound), with real queuing (load avg ~2x core count by the end of the run).
- **`py-spy top --pid <master-pid> --subprocesses`** during k6 load: initially misleading —
  `_worker (concurrent/futures/thread.py)` dominated every snapshot (~1200% own time, growing
  linearly with wall time). Diagnosed via the flamegraph (`py-spy record`) that this was a **false
  lead**: `_worker` branches off as a sibling of Gunicorn's own arbiter loop, straight from
  `_bootstrap_inner` — and critically, it showed up even on the **master** process (PID 307782),
  which never touches an HTTP request. Confirmed via `--subprocesses` mixing the master's internal
  thread-bookkeeping into the same graph as real request work — an artifact of profiling all
  subprocesses together, not part of the request path at all.
- **Re-ran `py-spy record` against a single worker PID only** (no `--subprocesses`) — clean
  flamegraph of just request handling. Zoomed progressively: `routing.py` → **`solve_dependencies`**
  was the single largest branch under routing (comparable in width to the actual DB
  fetch/execute/write branch) → `request_params_to_args` → three roughly-equal
  `_get_multidict_value` calls, one per query param (`region`, `limit`, `cursor`).
  **Finding: a comparable share of per-request CPU time is spent in FastAPI's dependency-injection
  parameter-resolution machinery — extracting 3 query params — as is spent on the actual Postgres
  round-trip.**
- **Reasoned through why Redis wouldn't be the right next move even before testing:** Postgres's
  `shared_buffers`/OS page cache already serves hot rows from memory (consistent with the ~2ms
  `EXPLAIN ANALYZE` result), so for this access pattern Redis and Postgres both resolve to an
  in-memory lookup — Redis's usual win (avoiding disk I/O) isn't available to claim here. Decided
  **not to pursue Phase 2 (Redis) for now** — correctly identified the bottleneck is framework
  per-request overhead, not the data-fetch layer, so caching wouldn't address the largest branch.
- **First fix attempt (falsified):** removed `response_model=list[FeedItem]`, returned a raw
  `JSONResponse([dict(r) for r in rows])` instead of relying on Pydantic response validation.
  Re-ran k6: **no improvement** — p99 stayed ~1.53s (from 1.42–1.61s baseline). Correctly reasoned
  why: the route's parameters (`region: str`, `limit: int = Query(...)`, `cursor: Optional[int]`)
  were untouched, so `solve_dependencies`/`request_params_to_args` — the actual largest branch —
  still runs unchanged on every request. This only removed the smaller response-side
  (`dump_json`/`validate_python`) cost, not the largest one. Confirms the profiling diagnosis was
  accurate (a real falsification test, not just a guess).
- **Next planned attempt:** bypass FastAPI's per-field `Query()` dependency injection by accepting
  the raw Starlette `Request` object and reading `request.query_params` directly instead of
  declaring `region`/`limit`/`cursor` as individual function parameters — this targets
  `solve_dependencies` itself, the branch actually measured as largest. Tradeoff to resolve:
  manual type coercion/bounds-checking for `limit`/`cursor` (currently free via `Query(ge=1, le=100)`)
  vs. FastAPI's automatic OpenAPI docs/validation for those params.

### Phase 1 — 2026-09-12 session: raw-request fix tested, pool re-tuned, and the CPU-ceiling verdict
- **Rewrote `get_feed`** to accept the raw Starlette `Request` and read `region`/`limit`/`cursor`
  from `request.query_params` directly instead of FastAPI's `Query()`-injected parameters,
  bypassing `solve_dependencies` for this route (user wrote this by hand, per learning-mode).
  A `response_model=list[FeedItem]` slip-back was caught and re-removed mid-session.
- **k6 result: real but modest win.** p99 dropped from the ~1.4–1.61s baseline range to
  **~1.34–1.38s** across repeated runs. Confirmed via `py-spy record` on a single worker PID that
  the `fastapi/dependencies/utils.py` (`solve_dependencies`) branch shrank/disappeared for this
  code path — the fix did what the flamegraph predicted.
- **Reasoned explicitly about the size of the win**: since `EEXPLAIN`-cheap DB fetch was only
  ~2.7-2.85% of total samples in the new flamegraph, and dependency resolution was the removed
  cost, the modest overall improvement (not larger) is consistent with several other
  comparably-sized costs still remaining (see below) — not evidence the fix "didn't work."
- **New flamegraph discovery**: a previously unnoticed branch —
  `app (routing.py:145)` → `sender` → `send` → Python's `logging` module
  (`info` → `_log` → `makeRecord` → `handle` → `emit`) — is Gunicorn's `--access-logfile` writing a
  line per request, and it's comparably sized to the other remaining costs (DB round-trip,
  response serialization). Not yet acted on; flagged as a candidate for a future free experiment
  (disable/reduce access logging under load).
- **Pool `max_size` re-tuned with real headroom math, not a guess.** Checked
  `SHOW superuser_reserved_connections;` (3) alongside `SHOW max_connections;` (100) → 97 available
  to non-superuser roles. Correctly rejected the naive `25 × 4 workers = 100` (would leave zero
  headroom for an admin `psql` session or health check) in favor of **`max_size=22` per worker
  (22 × 4 = 88 total)**, leaving real margin. Also correctly flagged that this per-worker number is
  fragile if `--workers 4` ever changes — worth revisiting if the worker count changes.
- **Effect of the pool change: present but small and noisy.** p99 hovered ~1.4s after the change,
  within the same noise band as recent runs (throughput swung ~860–1120 req/s across otherwise
  similar configurations). Correctly reasoned this doesn't mean "no effect" — it means any real
  effect is being capped by CPU saturation (0% idle across 4 cores), not by connection
  availability. A resource-starved-on-one-axis system won't show a clean win from fixing a
  different, non-bottleneck axis.
- **Isolated-variable re-test**: deliberately reverted to the `Query()`-based param resolution
  (reintroducing `solve_dependencies`) to test the pool-size change on its own, separate from the
  request-params fix — good methodology (change one variable at a time). Result: p99 1.8s, throughput
  ~860 req/s — worse than other recent runs, but attributed correctly to combined effects/run
  variance rather than treated as a real regression signal on its own.
- **PgBouncer question resolved by reasoning, not by installing it.** Recognized that the
  `pool.py` (`asyncpg`) contention visible in the flamegraph is the app's *own in-process* pool
  being exhausted by concurrent coroutines in a single worker — a wait that happens entirely
  before a query ever reaches Postgres. PgBouncer sits *between* app and Postgres and protects
  Postgres's physical connection count; it would not touch this specific in-process wait.
  Correctly concluded that raising `asyncpg`'s own `max_size` (free, one-line) was the right first
  test, not introducing PgBouncer as a new service — PgBouncer remains a legitimate later step only
  once local `max_size` is pushed near Postgres's real ceiling across all workers/replicas.
- **Replicas + load balancer verdict.** Confirmed host has only 4 physical CPU cores
  (`nproc` inside the container already matched this). Concluded that adding replicas or more
  Gunicorn workers on the *same* host would not add real capacity — it would divide the same
  already-saturated 4 cores further. Real horizontal scaling requires additional hardware
  (a second machine, a cloud instance) and was **not tested locally**, correctly identified as
  outside what this hardware can prove.
- **Overall verdict for Phase 1**: CPU is saturated with no single remaining dominant cost —
  dependency resolution, DB round-trip, response serialization, and access logging are now all
  roughly comparable, small costs. Further code-level micro-optimization has diminishing returns
  relative to the cost (lost type safety/validation from bypassing `Query()`). The honest next
  lever is more compute (horizontal scaling across real additional hardware), not more tuning.

### Not started yet
- Decide next move: push further on tuning (more workers / check Docker's own CPU allocation to
  the container) vs. treat current state as "proven Postgres+API alone has a real ceiling" and
  move to Phase 2 (Redis) — per the plan, this is exactly the trigger point for adding a cache.
- PgBouncer: deprioritized (see 2026-09-12 reasoning above) unless local `asyncpg` `max_size` is
  pushed near Postgres's real connection ceiling across all workers/replicas in the future.
- Horizontal scaling (more replicas + load balancer): identified as the real next lever, but
  requires additional hardware not available to test on this machine (host has only 4 physical
  cores, already saturated). Revisit if/when more compute becomes available.
- Candidate free experiment not yet tried: disabling or reducing Gunicorn's `--access-logfile`
  writes under load, since the flamegraph showed this as a comparably-sized per-request cost to
  the other remaining ones (see 2026-09-12 session above).
- rss_poller.py is still run manually, one-shot — not yet running continuously in the background
  or containerized.

### Learning material saved to Obsidian (`Learning/Backend Engineering/Python/FastAPI/`)
- `fastapi-phase1-learnings.html` — visual summary, created after the first load-test run (covers
  Docker/Compose wiring, k6 basics, initial 10.93s p99 result only).
- `2026-09-08-live-feed-engine-phase0-phase1.html` — full learning report (via `/explain-diff-html`)
  covering the whole arc from Phase 0 through the complete Phase 1 diagnosis chain, with Background/
  Intuition/Code/Quiz sections and the before→after k6 numbers at each step.
- `2026-09-08-live-feed-engine-architecture-evolution.html` (+ matching `.md`) — interactive
  click-through architecture diagram (via the architecture-diagram skill), 5 stages (0–4) each with
  its own request-path topology; Stage 4 auto-switches from "Single Process" to "Multi-Worker" view;
  diagnostic tools shown as their own node in Stage 3.
- `live-feed-engine-command-cheatsheet.html` — reference sheet of every command used so far (local
  dev/venv, Docker & Compose, psql/`EXPLAIN ANALYZE`, seeding, k6), plus the typical diagnosis
  sequence (k6 → docker stats → EXPLAIN ANALYZE → fix → rebuild → re-test).
- `4-cpu-profiling-command-cheatsheet.html` — command reference for the 2026-09-10 CPU profiling
  session (`top`, `docker top`, `py-spy top`/`record`, master-vs-worker PID distinction).
- `5-live-feed-engine-cpu-diagnosis-architecture.html` (+ matching `.md`) — interactive
  click-through architecture diagram (via the architecture-diagram skill) covering the full
  2026-09-10 → 2026-09-12 diagnosis arc: Baseline Bottleneck → Worker + Pool Fix →
  CPU Diagnosis (py-spy) → Experiments & Verdict, with a Before/After Fix mode toggle.
- `6-session-summary-cpu-ceiling.html` — full visual session summary of the 2026-09-12 session:
  p99 through-line, every command run, the falsified vs. confirmed experiments, and the lessons
  worth keeping (master-vs-worker profiling, falsification over guessing, reasoning before
  reaching for a tool, run-to-run noise, and the hardware-ceiling check before scaling out).

### Next session: pick up here
Phase 1 diagnosis is essentially complete — decide how to spend effort next:
1. **Quick/free experiment left on the table**: disable or reduce Gunicorn's `--access-logfile`
   writes under load and re-run k6, since it showed up as a comparably-sized per-request cost.
   Cheap to test, low expectation of a large win given everything else is now similarly small.
2. **Decide whether to accept current performance as this hardware's ceiling** (p99 ~1.34-1.4s,
   down from 10.93s baseline — a ~7-8x improvement overall) and move on to other parts of the
   system (rss_poller containerization, new features), vs. continuing to chase diminishing
   code-level returns.
3. **Horizontal scaling** (more replicas + load balancer) is the identified real next lever for
   further throughput, but requires additional hardware beyond this 4-core host — not testable
   locally. Revisit only if/when more compute becomes available (e.g. a cloud VM).
4. Redis (Phase 2) remains deprioritized — Postgres already serves hot rows from memory at
   comparable speed, confirmed by reasoning and by `EXPLAIN ANALYZE`. Revisit only if a genuine
   I/O-bound cost re-emerges (e.g. a much larger dataset that no longer fits in `shared_buffers`).
5. PgBouncer remains deprioritized — the connection contention observed was in the app's own local
   `asyncpg` pool, not Postgres's connection ceiling. Revisit only once local `max_size` is pushed
   near Postgres's real limit across all workers/replicas.
6. rss_poller.py is still run manually, one-shot — not yet running continuously in the background
   or containerized.
