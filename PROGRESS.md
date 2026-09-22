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

### Phase 1 — closed out (2026-09-12)
- Confirmed Gunicorn's `CMD` never included `--access-logfile` in the first place — the "free
  experiment" from the previous session was already effectively done, just not logged as such.
- **Decision: accept current p99 (~1.34–1.4s, down from a 10.93s baseline) as this hardware's
  ceiling.** Not pursuing further code-level tuning — diminishing returns confirmed by reasoning
  (dependency resolution, DB round-trip, serialization, and access logging are all now similarly
  small costs, no single dominant one left).
- **Horizontal scaling reasoning exercise (not yet run):** worked through whether capping a
  container to `--cpus=2` and running two replicas behind a load balancer on this same 4-core host
  would help. Correctly reasoned through to the answer without running it: total physical compute
  is conserved (Docker's `--cpus` is a CFS quota, not dedicated silicon), so combined throughput
  across the two capped containers should land at the same ceiling as the current single
  4-worker/4-core container — or worse, once the load balancer's own latency/hop overhead is
  added. True horizontal scaling requires genuinely additional hardware (a second machine, a free-
  tier cloud VM); confirmed there's no way to fake that gain on a single 4-core box. Logged as a
  reasoned-but-untested conclusion; left un-run by choice to move on to `rss_poller.py`.
- PgBouncer and Redis (Phase 2) remain deprioritized per the 2026-09-12 reasoning above — no new
  information changes either verdict.

### rss_poller.py — containerized (2026-09-12)
- Confirmed `main()` already had a `while True: poll_once(); sleep(300)` loop from earlier work —
  the "run manually" status only meant `poll_once()` had been invoked directly, not that the
  continuous-loop structure was missing.
- `DB_DSN` switched to `os.environ.get("DATABASE_URL", "postgresql://...localhost...")`, same
  env-var-with-fallback pattern as `main.py`.
- **Decided no `asyncpg` pool needed here** (reasoned, not assumed): the poller is a single
  sequential process with one connection and no concurrent requests, unlike `api`'s many
  concurrent HTTP requests — pooling solves a concurrency problem this process doesn't have.
- **Separate Dockerfile + requirements per service, not a shared one.** Reasoned that `api` and
  `rss_poller` have different `CMD`s and dependency sets (poller doesn't need `fastapi`/
  `gunicorn`/`uvicorn`), and a shared Dockerfile handling two images invites confusion for anyone
  reading it later. Added `Dockerfile.poller` (same base image, installs `requirements-poller.txt`
  — just `feedparser`+`psycopg2-binary`, no `EXPOSE`/ulimits since there's no HTTP concurrency) and
  `requirements-poller.txt`.
- Caught and fixed a copy/paste bug mid-session: `Dockerfile.poller` initially `COPY`'d `main.py`
  instead of `rss_poller.py` while the `CMD` still ran `rss_poller.py` — fixed to copy the right
  file.
- Added `rss_poller` service to `docker-compose.yml`: built from `Dockerfile.poller` via
  `build.context`/`dockerfile:` override, same `DATABASE_URL` as `api`, `depends_on: postgres:
  condition: service_healthy`, `restart: unless-stopped` (reasoned: an unhandled exception from a
  flaky feed fetch shouldn't leave the poller dead — restart is the appropriate policy here, same
  as `api`).
- **Verification caught a real gotcha, not just a happy-path check**: `docker logs rss_poller`
  showed nothing despite the poller having clearly inserted rows (confirmed via `SELECT count(*)`
  climbing). Diagnosed as Python's stdout switching from line-buffered to block-buffered when not
  attached to a TTY (a container's stdout is piped, not a terminal) — the classic
  "print() invisible in Docker logs" gotcha. Fixed via `python -u rss_poller.py` in `CMD`. After
  the fix, `docker logs -f rss_poller` showed live per-publisher polling output
  (`Polling BBC World.... -> 23 entries processed`, etc.) confirming the 5-minute loop actually
  cycles, not just that one poll happened.
- **End-to-end confirmed working**: separate container, own trimmed image, auto-restarts on
  failure, independent 5-minute polling loop against all 4 real publishers, row count climbing
  live in `docker logs -f`.
- Known open issue, flagged this session, **now fixed** (see next section): `insert_article` had
  no dedup/conflict handling on `url` — every `poll_once()` re-fetched the same feed entries and
  re-inserted them as new rows each cycle.

### rss_poller.py — duplicate-row fix (2026-09-12)
- **Diagnosed root cause**: `insert_article` used a plain `INSERT`, so every 5-minute poll cycle
  re-inserted every still-live feed entry as a brand-new row — no identity check on `url` at all.
  Confirmed via `SELECT ... ROW_NUMBER() OVER (PARTITION BY url ORDER BY id)` that real duplicates
  (e.g. `blinkenlights.de`, 6 copies) were already accumulating, while the synthetic seed data
  (`example.com/article/N`) was all unique — the duplication was entirely from the poller re-fetch
  pattern, not the seed.
- **Chose DB-level enforcement over app-level check-then-insert**, reasoned explicitly: a
  "`SELECT` to check, then `INSERT` if absent" from app code has a TOCTOU (time-of-check-to-time-
  of-use) race — a gap between the check and the write where a duplicate could still land — plus
  it costs two round trips per row. A `UNIQUE` constraint enforced atomically by Postgres closes
  that gap and is a single round trip via `ON CONFLICT`.
- Added `UNIQUE` on `article.url` in `init.sql` (schema-of-record for future fresh volumes).
- **Live-table migration required a two-step fix, not a straight `ALTER TABLE`**: the constraint
  add failed first try (`ERROR: could not create unique index ... Key (url)=(...) is duplicated`)
  because 418 real duplicate rows already existed from the un-deduped polling. Used a `WITH
  duplicate_rows AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id) AS row_num FROM
  article) DELETE FROM article WHERE id IN (SELECT id FROM duplicate_rows WHERE row_num > 1);` CTE
  to remove all but the earliest (`ORDER BY id`) copy of each duplicated `url`, deleting 418 rows,
  then re-ran `ALTER TABLE article ADD CONSTRAINT uq_url UNIQUE (url);` successfully.
- Updated `insert_article`'s `INSERT` to `... ON CONFLICT (url) DO NOTHING` so a re-seen `url` on a
  future poll cycle silently no-ops instead of throwing `UniqueViolation` and crashing the loop.
- **Verified end-to-end**: rebuilt `rss_poller`, ran a full cycle, confirmed via `docker logs -f`
  no crash and via `SELECT count(*)` that already-seen URLs stopped adding new rows — only genuinely
  new feed entries increment the count now.

### Not started yet
- Decide next move: push further on tuning (more workers / check Docker's own CPU allocation to
  the container) vs. treat current state as "proven Postgres+API alone has a real ceiling" and
  move to Phase 2 (Redis) — per the plan, this is exactly the trigger point for adding a cache.
  (Current lean, per 2026-09-12 session above: accept the ceiling, Redis/PgBouncer stay
  deprioritized.)
- Horizontal scaling (more replicas + load balancer): identified as the real next lever, reasoned
  through conceptually (see above) but deliberately left untested — requires additional hardware
  (second machine or free-tier cloud VM) not available locally. Revisit if/when more compute
  becomes available.

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
1. ~~**Quick/free experiment left on the table**: disable or reduce Gunicorn's `--access-logfile`
   writes under load and re-run k6~~ — **done**: confirmed (2026-09-12 session, "Phase 1 — closed
   out") that Gunicorn's `CMD` never included `--access-logfile` in the first place, so this
   experiment was already effectively satisfied, just not logged as such at the time.
2. **2026-09-16 decision: accept current performance as this hardware's ceiling** (p99 ~1.34-1.4s,
   down from 10.93s baseline — a ~7-8x improvement overall) and move to the next phase rather than
   continuing to chase diminishing code-level returns. Re-confirmed Redis wouldn't be worth adding
   *at current scale*: same reasoning as before (Postgres already serves hot rows from
   `shared_buffers`/OS cache at ~2ms, so Redis would just be another in-memory hop with no I/O to
   save) — conditional "add it if it demonstrably helps p99, otherwise skip" test comes back
   negative on this dataset size. Revisit only if a genuine I/O-bound cost re-emerges (e.g. a much
   larger dataset that no longer fits in `shared_buffers`).
3. **Horizontal scaling** (more replicas + load balancer) is the identified real next lever for
   further throughput, but requires additional hardware beyond this 4-core host — not testable
   locally. **Deferred to a separate cloud-focused session** (this session is moving on to the next
   phase instead).
   - **2026-09-16 decision: plan is Oracle Cloud Free Tier**, not AWS/Azure. Reasoned through the
     free-tier options: AWS (`t2.micro`/`t3.micro`, 1 vCPU/1GB, 12-month) and Azure (`B1s`, 1
     vCPU/1GB, 12-month) are both too small to be a trustworthy second node — 1 vCPU would bottleneck
     immediately on its own and wouldn't isolate whether horizontal scaling itself helps. Oracle's
     free tier is **permanently free** (not 12-month) and offers **Ampere A1 (ARM)** instances up to
     4 vCPUs / 24GB RAM total, enough to split into 2 real nodes with meaningful cores each — the
     only one of the three that makes the "does a second real machine raise the ceiling" test fair.
     User is checking out Oracle Cloud signup/setup separately; not yet provisioned. **Pick up here
     in the next cloud-focused session**: provision 2 Oracle A1 nodes, put a load balancer in front,
     re-run `k6/load_test.js` against the pair, compare p99 against the single-host 1.34–1.4s
     baseline.
4. PgBouncer remains deprioritized — the connection contention observed was in the app's own local
   `asyncpg` pool, not Postgres's connection ceiling. Revisit only once local `max_size` is pushed
   near Postgres's real limit across all workers/replicas.

### 2026-09-16 — decided: Phase 2 (Redis) needs a synthetic hot-key/high-volume load tier first
- **Reasoning**: the plan's Phase 2 goal is a before/after Redis graph, per the system-design case
  study (`Learning/System Design/Practice/News Aggregator/part1_news_aggregator.html`,
  `part2_news_aggregator.html`) that assumes ~100M DAU with spikes to ~500M. Real traffic on this
  project (4 RSS publishers polled every 5 min, uniform random-region k6 test) never gets close to
  that, and won't organically — so adding Redis against current load would be speculative, not
  proven, exactly what Phase 1's "don't add Redis speculatively, prove you need it first" warns
  against.
- **Resolution**: don't chase real user/data growth (there isn't any) — simulate the case study's
  scale synthetically, the same way Phase 1's `seed.py`/`k6/load_test.js` already stood in for real
  load. Concretely, before Phase 2 starts:
  1. Bump seed volume well past the current 1M rows (e.g. 10M–50M) and/or skew region distribution
     so a few regions dominate, instead of the current uniform spread across all regions.
  2. Add a **hot-key k6 scenario** alongside the existing random-region one — most VUs hitting one
     or two regions — since that's the access pattern a cache actually helps with; a uniform-random
     test spreads load evenly and structurally can't show a caching win.
  3. Re-run k6 against this new load tier on the current Postgres-only setup and confirm it actually
     degrades on hot keys (re-creating Phase 1's "feel the pain" step, but for cache-shaped pain —
     repeated hot reads — rather than the CPU-parallelism pain already solved).
  4. Only once that degradation is shown does Phase 2 (add Redis, compare graphs) become a real
     "prove it helps" exercise instead of cargo-culting the plan.
- Same logic will apply later to justifying Phase 3 (Kafka/outbox): that needs a simulated **write
  burst** (e.g. a burst-producer script hammering inserts, mimicking a breaking-news spike), not
  organic traffic, to justify decoupling ingestion from cache-serving.
- **Step 1 done (2026-09-16/17): `seed.py` chunked and 50M rows seeded.**
  - Diagnosed the original `seed.py`: built the entire CSV for `count` rows into one `io.StringIO`
    buffer before a single `copy_expert` call — for 50M rows this filled available RAM (10.7GB
    free of 16GB total) during a live run, confirming the concern before touching the code.
  - Fix (user-written, via `islice`-based batching, arrived at through Socratic back-and-forth):
    added `batch_generator(rows, batch_size)` — `while batch := list(islice(rows, batch_size)):
    yield batch` — and `main()` now loops over 1M-row batches, building/flushing/`copy_expert`-ing
    one small buffer per batch (`buf.seek(0); buf.truncate(0)` between batches) instead of one
    50M-row buffer.
  - **Key insight surfaced during design**: because `generate_rows(count, publisher_ids)` is called
    *once* and `islice` just carves pieces off that same shared generator, the `i` used for
    `Synthetic Article {i}` / `https://example.com/article/{i}` keeps counting globally across all
    batches (0 → 49,999,999) — sidesteps the per-batch-offset problem that coming at this via
    "call `generate_rows(batch_size,...)` fresh per batch" would have hit (duplicate URLs into the
    `UNIQUE (url)` constraint, since `i` would reset to 0 each call). Also naturally handles a
    non-evenly-divisible remainder batch for free (the final `islice` just returns whatever's left,
    however small, and the `while` loop stops on an empty list) — no manual `math.ceil`/`min`
    capping needed once `islice` is shared across one generator instance.
  - **Verified**: `SELECT count(*) FROM article;` confirms 50,000,000 rows; RAM stayed within
    available limits throughout the run (watched via `htop`/`free -h`), no swelling/OOM like the
    pre-chunking attempt.
- **Next**: hot-key k6 scenario (skew requests toward 1-2 regions instead of uniform-random) against
  this 50M-row table, to see whether Postgres alone actually degrades at this volume+access-pattern
  before touching Redis.

### 2026-09-17 — uniform-random re-test at 50M rows, then hot-key k6 scenario, then verdict on Redis
- **Uniform-random re-test at 50M rows** (same `k6/load_test.js` as the original 1M-row runs, no
  code changes): p99 **1.67-1.68s**, vs. the ~1.34-1.4s baseline at 1M rows — a real but modest
  increase. `EXPLAIN ANALYZE` on `region='US'` at 50M rows: still an **Index Scan Backward** on
  `article_pkey`, **Execution Time 0.086ms** — confirmed the query itself did not get more
  expensive with 50x the data (consistent with cursor-pagination-on-indexed-PK not degrading with
  table size, same conclusion as the original Phase 1 diagnosis). Correctly reasoned the p99 bump
  is consistent with the already-known CPU-bound framework-overhead ceiling (this `main.py` is the
  `Query()`-injected/`response_model=list[FeedItem]` version, not the raw-`Request` experiment from
  the earlier session — so `solve_dependencies` + Pydantic validation are both live costs again),
  not a DB-scaling problem.
- **Hot-key k6 scenario added** (user-written, via Socratic back-and-forth): weighted-random region
  selection using a cumulative-probability walk (`US: 0.7, EU: 0.2, APAC: 0.07, LATAM: 0.03`) —
  iterate `[{region, value}, ...]` with `for...of`, accumulate a running total, pick the first
  region whose cumulative weight exceeds a single `Math.random()` draw. Caught and fixed two real
  bugs along the way: `Math.floor(Math.random())` (always 0, `Math.floor` of anything in `[0,1)` is
  0) → fixed to bare `Math.random()`; and `for...in` over the `REGIONS` array giving string indices
  (`"0","1",...`) instead of the actual `{region, value}` objects → fixed to `for...of`.
- **Verified the weighting actually worked**, not just assumed: added a k6 `Counter("region_counter")`
  tagged per region (`.add(1, { region: region.region })`), plus one per-region threshold entry
  (`'region_counter{region:US}': ['count>=0']`, etc.) — a deliberately unfailable threshold whose
  only purpose is forcing k6's text summary to print each tagged submetric's line (k6's default
  summary doesn't auto-break-down custom metrics by tag otherwise; learned via the "thresholds on
  submetrics" doc). **Result matched the intended 70/20/7/3 weights almost exactly**: US 95768/136883
  = 69.98%, EU 27542/136883 = 20.12%, APAC 9539/136883 = 6.97%, LATAM 4034/136883 = 2.95%.
- **Hot-key run result**: p99 **1.83s** — a small bump from the uniform-random 50M-row run
  (1.67-1.68s), nowhere near the dramatic "Postgres falls over under hot-key load" result the
  system-design case study's ~100M DAU premise assumes.
- **Root-cause reasoning for why hot-keying didn't matter (Socratic, arrived at by the user with one
  assist)**: two contributing factors, then the fundamental one.
  1. Postgres's buffer pool / OS page cache already keeps the hot working set in memory — every
     query is `ORDER BY id DESC LIMIT 20`, so it only ever touches the same narrow "newest ~20 rows
     per region" slice regardless of how many times or how heavily that region is hit. Skewing
     access doesn't enlarge that slice.
  2. **The fundamental reason**: the identified bottleneck (Phase 1, `py-spy`/`docker stats`) is
     **CPU saturation from fixed per-request framework overhead** (`solve_dependencies`, Pydantic
     validation, JSON serialization), not the DB layer. That cost is a function of *how many
     requests/sec arrive*, not *which data or how repeated the data is* — so shifting the traffic
     distribution across regions leaves total request volume and per-request CPU cost unchanged,
     and the same 4 cores stay equally saturated either way. The earlier multi-worker/multi-core fix
     (Phase 1) *raised* the CPU ceiling (10.93s → ~1.4s) but didn't remove the CPU-bound nature of
     the bottleneck — `docker stats`/`top` still showed 0% idle across all 4 cores under load.
- **General principle surfaced**: a cache only pays off when (a) the underlying fetch/compute is
  genuinely expensive, and (b) many requests overlap on the same data so the cache is actually
  reused. Here, condition (a) is false — the query is a sub-millisecond indexed lookup, already
  effectively cached by Postgres itself — so no amount of access-pattern skew can make caching it
  worthwhile. Repeating a cheap operation stays cheap.
- **Verdict: the hot-key experiment succeeded, not failed** — it correctly demonstrated that Redis
  would not help `/feed` as currently built, no matter the traffic skew, closing the loop opened by
  the "does Redis help without more data/users" question from the 2026-09-16 session. Redis stays
  deprioritized. It would only become relevant if the actual bottleneck shape changed — e.g. a
  genuinely expensive per-request computation (complex ranking/aggregation, a slow downstream call),
  or a dataset large enough to no longer fit in `shared_buffers`/OS cache (real disk I/O on "hot"
  reads) — neither of which is true of this endpoint today.
- **Superseded by the 2026-09-17 `/feed/full-scan` experiment below** — the "skip Redis vs. simulate
  expensive cost" fork was resolved by actually building the expensive-cost simulation, which finally
  produced a genuine justification for Redis rooted in a real failure, not just reasoning.

### 2026-09-17 — shared_buffers/memory-limit experiment, then the genuinely expensive `/feed/full-scan` endpoint
- **Attempted shrinking `shared_buffers` first, reasoned out of it before testing**: found
  `shared_buffers` was already at Postgres's default (128MB) against a 12GB `article` table
  (`pg_total_relation_size`) — already <1% of the table fits in Postgres's own buffer pool, yet
  queries stayed sub-millisecond. Correctly concluded the **OS page cache** (not `shared_buffers`)
  was serving hot reads, since the host has ~10.7GB free RAM independent of Postgres's internal
  setting — so shrinking `shared_buffers` alone wouldn't force real disk I/O. Pivoted to constraining
  the **container's total memory** instead (`deploy.resources.limits.memory: 512M` on the `postgres`
  service in `docker-compose.yml`) — verified actually enforced via `docker stats` (`324.7MiB /
  512MiB` ceiling shown live, not silently ignored despite `deploy.resources` historically being a
  Swarm-only setting).
- **First attempt at forcing cache misses (deep-pagination k6 scenario) also didn't break anything**,
  and correctly reasoned out why before concluding "the memory limit doesn't work": added a random
  `cursor` (uniform across the full 50M-row ID range) to `k6/load_test_deep_pagination.js`, replacing
  "always fetch the newest rows." Result: p99 **1.84s**, indistinguishable from the earlier hot-key
  result (1.83s); memory only reached 324MB of the 512MB limit — never even filled up. **Root cause
  traced by hand**: the query is `WHERE region=$1 AND id<$2 ORDER BY id DESC LIMIT 20` — since 1-in-4
  rows match a given region, Postgres only needs to examine ~80 rows (4 × the 20-row limit) to satisfy
  any single request, *regardless of where in the 50M-row range the cursor lands*. Randomizing the
  cursor changes *which* ~80 rows get touched, not *how many* — so even a fully memory-constrained,
  fully-randomized-cursor test can't produce real per-request cost when every query is `LIMIT`-bounded
  and indexed. Confirmed via real Block I/O this time (2.59GB read, vs. near-zero in prior runs) —
  genuine disk I/O did occur cumulatively across requests, but each individual request stayed too
  cheap to matter, so latency didn't move.
- **Correctly identified the fix**: to force genuine per-request DB cost, the query itself needs to be
  unable to stop early — i.e., drop `LIMIT` (or raise it enormously) so a single request has to
  fetch/sort/return a large result set, not just walk ~80 rows and stop.
- **Added `/feed/full-scan?limit=`, a new dedicated endpoint** (not a change to the real `/feed`
  contract, since real users would never want an unbounded feed) — `Query(1_000_000, le=10_000_000)`,
  no `region` filter, raw `JSONResponse` instead of `response_model=list[FeedItem]` (deliberately
  keeping Pydantic out of this specific measurement). Two real bugs caught and fixed before it worked:
  1. **`SELECT *` across the `article`/`publisher` join** — both tables have a column named `id`;
     Postgres expands `SELECT *` left-to-right, so `dict(r)` silently let the *publisher's* `id`
     overwrite the *article's* `id` (dict construction keeps the last-seen key). Fixed by explicitly
     aliasing columns, same pattern as the original `/feed` query, instead of `SELECT *`.
  2. **`TypeError: Object of type datetime is not JSON serializable`** — bypassing
     `response_model=list[FeedItem]` also bypassed Pydantic's automatic `datetime → ISO-8601` string
     conversion (a gotcha already known from Phase 0, but only realized here because this is the first
     endpoint that skips Pydantic on the response side entirely). Fixed with
     `dict(r) | {"publishedAt": r["publishedAt"].isoformat()}` per row.
- **Manual curl baseline, before any load testing**: single unconcurrent requests already showed a
  real cost curve — `limit=100_000` → 21.3MB response, 0.91s; `limit=1_000_000` → 212.7MB response,
  **9.7s**, for one request with zero concurrency. First time in this project a single request alone
  (no load test needed) demonstrated meaningfully expensive behavior.
- **k6 scenario for `/feed/full-scan`** (`k6/load_test_deep_pagination.js`, repurposed): reasoned
  through why the existing `ramping-vus`-to-1000 shape and `p(99)<200` threshold made no sense for an
  endpoint whose single-request baseline is already multi-second — switched to `constant-vus` (10
  VUs, fixed, no ramp), 90s duration, threshold dropped entirely, and `limit` picked randomly per
  iteration from `[100_000, 300_000, 600_000, 800_000, 1_000_000]`. Caught a real bug mid-build: a
  leftover `cursor` variable (from the deep-pagination version) was being passed as `limit` in the
  URL instead of the actual randomly-picked limit value — fixed to use the correct variable.
- **Result: real failures, not just slow numbers.** At just 10 concurrent VUs: Gunicorn workers hit
  `WORKER TIMEOUT` (default 30s) and were killed mid-request, which cascaded into Postgres logging
  `could not send data to client: Connection reset by peer` / `connection to client lost` (the
  killed worker's DB connection dropped mid-transfer). k6 itself hit request timeouts (60s) on several
  `limit=800_000`/`1_000_000` iterations. **22.85% of checks failed outright** — the first real
  failures (not just degraded latency) anywhere in this entire project's load testing.
  `http_req_duration`: avg 23.78s, median 15.77s, max 60s (timeout ceiling). `feed_api` memory spiked
  to **4.38GiB** at one point; `feed_postgres` stayed comfortably inside its 512MiB limit (264-307MB)
  throughout.
- **Root-cause reasoning (Socratic, user-driven)**: since Postgres never approached its memory
  ceiling but the API container spiked to 4+GB, the failure is rooted in **`main.py`'s handling of
  the result**, not Postgres itself — `conn.fetch()` fully materializes up to a million
  `asyncpg.Record`s, each gets converted to a dict, each `publishedAt` gets `.isoformat()`-converted,
  all assembled into one giant Python list, then `json.dumps`-encoded into a single ~213MB string —
  all in-process, all before a single response byte is sent. This is a genuinely expensive
  computation, finally satisfying condition (a) of the caching principle established during the
  hot-key experiment.
- **Correctly reasoned through what a cache would and wouldn't fix**: caching raw DB rows would only
  save the DB round-trip (a small slice of total cost); caching the **already-fully-serialized JSON
  blob** would skip the DB fetch *and* the expensive dict-building/isoformat/json.dumps work entirely
  on a cache hit — correctly identified as the right thing to cache, not the raw rows. Also correctly
  identified the remaining gap: the current k6 script picks `limit` **uniformly at random** per
  iteration, so repeat requests for the *same* limit are rare — a cache would mostly miss under this
  access pattern. Concluded that a **weighted/skewed distribution over `limit` values** (same
  cumulative-probability technique already built for regions) is needed before Redis can actually
  demonstrate a hit-rate win, mirroring a real "some reports are more popular than others" pattern.
- **Verdict: this closes the loop opened on 2026-09-16.** Redis now has a real, self-demonstrated
  justification — not the original `/feed` endpoint (still correctly deprioritized, per the hot-key
  and volume experiments), but a new, deliberately-expensive endpoint (`/feed/full-scan`) that
  genuinely breaks under concurrent load today, on this hardware, with real worker timeouts and a
  4GB+ memory spike as evidence.
- **Next session: pick up here.**
  1. Weight `LIMITS` in `k6/load_test_deep_pagination.js` (e.g. `1_000_000` heavily favored, others
     rare) so repeated identical requests are common enough for a cache to show a real hit-rate
     effect — same cumulative-probability-walk pattern already built for `REGIONS`.
  2. Re-run the weighted version against the *uncached* `/feed/full-scan` first, to get a clean
     "before" baseline (expect similar worker-timeout/memory-spike behavior, now with a known skewed
     access pattern).
  3. Implement Phase 2 (Redis) scoped specifically to `/feed/full-scan`: cache the final serialized
     JSON blob per `limit` value (not raw rows), most likely with a TTL. This is also the natural
     place to demo the TTL/thundering-herd concept from the original plan (Phase 2's milestone).
  4. Re-run the same weighted k6 scenario against the cached version and compare — this is the
     legitimate "two load-test graphs, before/after Redis" the plan originally asked for, this time
     backed by a real breaking point instead of an assumed one.

### 2026-09-19 — weighted-limit k6 scenario built, "before" baseline captured, moving to Redis
- **Weighted `LIMITS` added to `k6/load_test_deep_pagination.js`**, same cumulative-probability-walk
  pattern as the region weighting: `100_000: 5%, 300_000: 5%, 600_000: 5%, 800_000: 70%, 1_000_000:
  15%` — `800_000` deliberately made the "popular report" a cache should demonstrably help with.
  Caught and fixed two real bugs before it worked, both via Socratic trace-by-hand (same method as
  the original region-weighting bugs):
  1. **Comparison backwards**: first attempt had `if (limit.p > runningTotal)` instead of
     `if (runningTotal > random)` — traced by hand and found the random draw (`random`) was never
     referenced in the condition at all, so `chosenLimitValue` stayed at its initial `0` for every
     single iteration, regardless of the weights. Fixed to match the working region-weighting pattern.
  2. **Threshold/tag mismatch**: threshold keys referenced `limit:100_000` (with underscores, matching
     the *source code's* numeric-literal syntax) and included a non-existent `limit:200_000`, while
     the actual tag values k6 stringifies are plain `100000` (underscores are just a JS numeric-literal
     separator, stripped at parse time — not part of the runtime value). Fixed thresholds to
     underscore-free values matching the real `LIMITS` array (`100000, 300000, 600000, 800000,
     1000000`).
  3. Also added a `Counter("limit_counter")` tagged per limit, mirroring the region counter — verified
     the weighting roughly held even over a small sample (31 total iterations, since each request
     takes many seconds): `800000` got 23/31 (~74%, close to the intended 70%), `1000000` got 3/31,
     `300000` got 3/31, `100000` got 2/31, `600000` got 0/31 (plausible at only 5% weight over 31
     draws).
- **"Before" baseline result (uncached `/feed/full-scan`, weighted limits, 10 VUs, 90s)**: **worse**
  than the earlier uniform-random run on every axis — **38.70% checks failed** (up from 22.85%),
  `http_req_duration` avg **30.27s**, median 28.1s, max 60s (k6's timeout ceiling). New, more severe
  failure mode observed this run: `Worker (pid:7) was sent SIGKILL! Perhaps out of memory?` — an
  actual kernel OOM-kill, one step worse than the previous run's Gunicorn-initiated `WORKER TIMEOUT`.
  Same Postgres-side cascade as before (`could not send/receive data from client: Connection reset by
  peer` → `connection to client lost`, triggered by the killed worker's connection dropping mid-transfer).
- **This is the "before" half of the before/after Redis comparison.** Next: design and implement the
  Redis caching layer for `/feed/full-scan`, scoped exactly as reasoned on 2026-09-17 — cache the
  final serialized JSON blob per `limit` value (not raw rows), then re-run this exact same weighted
  k6 scenario against the cached version and compare against today's numbers.

### 2026-09-19 (cont'd) — Redis implemented, but the "after" run was *worse*: a real thundering-herd/stampede finding
- **Infra added, piece by piece, each with a real bug caught before it worked**:
  - `docker-compose.yml`: new `redis` service (`redis:7-alpine`, no persistence volume — deliberate,
    so every test run starts from a clean/empty cache — and no memory cap yet, deferred until after
    a first working baseline). `api` service got `REDIS_URL: redis://redis:6379` and `depends_on:
    redis: condition: service_healthy`. Two bugs caught: `redis=7.4.1` in `requirements.txt` (single
    `=`, invalid pip syntax — fixed to `==`), and `depends_on: redis: condition: service_healthy`
    referencing a service with no `healthcheck:` block at all (Compose has nothing to evaluate for
    `service_healthy` without one) — fixed by adding a `healthcheck: test: ["CMD", "redis-cli",
    "ping"]` to the `redis` service, mirroring the existing Postgres healthcheck pattern.
  - `main.py`: `redis.asyncio` client wired into the existing `lifespan` pattern — `ConnectionPool
    .from_url(REDIS_DSN)` → `Redis.from_pool(pool)` on `app.state.pool_redis`, closed via `.aclose()`
    alongside the Postgres pool. One real bug caught via Socratic trace: first attempt called
    `redis.get(key)` / `redis.set(...)` **without `await`** on the async client — since a bare,
    un-awaited coroutine object is never equal to `None`, `if redis.get(key) != None` was *always*
    true, meaning the code took the "cache hit" branch on literally every request regardless of
    whether anything was ever cached. Fixed by adding `await` to both calls.
  - **Cache design landed on** (via one-question-at-a-time Socratic walkthrough, each step reasoned
    through rather than handed over): key = `fullscan:limit:{limit}` (namespaced by route + the one
    relevant param, since `/feed/full-scan` ignores region); value = the **already-fully-serialized
    JSON string** (`json.dumps(rows_modified)`), not raw rows — correctly reasoned that caching raw
    rows would only save the DB round-trip, while caching the finished blob also skips the expensive
    dict-building/isoformat/json.dumps work on a hit; cache hit returns via a plain `Response(content=
    cached_str, media_type="application/json")` rather than `JSONResponse(...)`, specifically to avoid
    double-serializing an already-serialized string; TTL `ex=20` chosen empirically-first (explicit
    "test it, adjust after" decision) after reasoning through the tradeoff between "long enough to
    reuse across a 90s test" and "short enough to actually observe an expiry event mid-test."
- **Manual curl verification (clean, no concurrency) — worked exactly as designed**: first request
  (cache miss) 1.045s / 22.7MB; identical second request (cache hit) **0.089s** / same 22.7MB — a
  **~12x** speedup, same payload, confirming the cached-blob mechanism itself is correct in isolation.
- **Re-ran the exact same weighted k6 scenario (10 VUs, 90s) against the now-cached endpoint —
  result was *worse* than the uncached "before" baseline, not better**: 66.66% checks failed (up
  from 38.70%), `http_req_duration` avg 41.8s / median 47.23s (up from avg 30.27s / median 28.1s),
  another kernel `SIGKILL` OOM. Weighting still held roughly as intended (`800000`: 16/26,
  `1000000`: 6/26).
- **Root-cause diagnosis (Socratic, user needed a direct explanation after initial guesses)**: this
  is a **cache stampede / thundering herd**, and a severe one, because request completion time under
  load (avg 41.8s) vastly exceeds the TTL (20s). With 10 concurrent VUs and no request-coalescing/
  locking around the cache-miss path, multiple VUs can check Redis for the same hot key (`800000`,
  70% weight) at effectively the same moment, all see "not cached yet," and all independently trigger
  the full expensive path (Postgres fetch → materialize hundreds of thousands of rows → build a
  ~200MB JSON string) *simultaneously* — then each of those duplicate computations **also** writes
  its own redundant ~200MB copy into Redis. Net effect: strictly *more* total memory pressure than
  the uncached baseline (N duplicate in-process blobs *plus* N duplicate Redis-side copies, vs. just
  N duplicate in-process blobs before), which explains both the higher failure rate and the more
  frequent/earlier OOM. This is exactly the "TTL + thundering herd" concept the original plan called
  out as a Phase 2 milestone — reproduced for real, in a more severe form than a textbook example
  because of the TTL-vs-completion-time mismatch identified above.
- **Correctly self-diagnosed partial explanation before assistance**: recognized that *sequential*
  timing (one request finishes, populates cache, later requests within the TTL window benefit) was
  sound reasoning — the gap was not realizing that *concurrent* arrivals during the population window
  bypass that entirely, since nothing coordinates simultaneous misses on the same key.
- **Not yet implemented — identified as the fix, deferred to next step**: stampede protection /
  request coalescing (e.g. a Redis `SET key value NX` used as a lock, so one request "claims" the job
  of populating the cache while concurrent others wait or fall back, instead of every miss
  independently redoing the full expensive work).
- **Next: re-run at lower concurrency (2 VUs instead of 10)** first, to see whether the cache
  delivers a clean win *without* the stampede confounding the result — isolating "does caching help
  the popular key" from "does caching survive concurrent cold-start misses," before circling back to
  implement stampede protection as its own deliberate experiment.
- **Low-concurrency (2 VUs, 90s) re-run: clean, unambiguous win — the first "before/after Redis"
  result this project set out to produce.** `checks_failed` **0.00%** (down from 38.70% uncached-at-
  10-VUs and 66.66% cached-at-10-VUs-with-stampede). `http_req_duration` avg **2.87s**, median
  **913.53ms** (down from avg 30.27s / median 28.1s uncached). Memory stayed contained throughout:
  `feed_api` never exceeded ~1GB (down from the 4.38GB spikes and OOM kills at 10 VUs),
  `feed_postgres` never exceeded ~200MB. Weighting held (`800000`: 41/63 ≈ 65%, close to the intended
  70%; `1000000`: 9/63; `300000`/`600000`: 5/63 each; `100000`: 3/63). `max=14.06s`/`p(90)=10.13s`
  still show some slow tail requests (plausibly the rarer `1_000_000` key's first-ever miss before
  anything is cached), but nothing close to the previous timeout/OOM territory.
- **Diagnosis confirmed empirically**: at low enough concurrency, the odds of multiple VUs colliding
  on the same *uncached* key at the same instant drop sharply, so the cache gets a real chance to
  populate once and then serve fast repeated hits — exactly the mechanism the design intended,
  without the stampede pathology from the 10-VU run masking it.
- **Verdict**: Redis now has a real, self-demonstrated "before/after" result — but scoped honestly:
  it works cleanly at low concurrency (2 VUs) and actively makes things worse at higher concurrency
  (10 VUs) without stampede protection. Both results are legitimate and worth keeping — this is a
  more honest, more production-realistic finding than a clean win at every concurrency level would
  have been (a cache is not automatically a good idea; it depends on whether cold-start/expiry
  windows are protected against concurrent duplicate work).
- **Next session: pick up here.**
  1. Implement stampede protection (e.g. a Redis `SET key value NX` lock around the cache-miss path,
     so only one concurrent request per key does the expensive work while others wait/fall back)
     specifically to make the 10-VU case behave like the 2-VU case.
  2. Re-run the 10-VU weighted scenario again after that fix and compare all three data points
     (10 VUs uncached, 10 VUs cached-no-protection, 10 VUs cached-with-protection) — this is the
     complete, honest version of the plan's "before/after Redis" milestone.
  3. Only after that: decide whether to add a memory cap to the `redis` service (deferred from
     2026-09-19's infra setup) now that real usage patterns/sizes are known from these runs.

### 2026-09-20/21 — stampede protection implemented (Redis lock + safe compare-and-delete release)
- **Dockerfile bug caught before the feature could even boot**: `Dockerfile` only had `COPY main.py .`
  — the new `scripts/` directory (holding the Lua release script) never made it into the image, so
  the container failed at import time with `ModuleNotFoundError: No module named 'scripts'`, crashing
  every Gunicorn worker on boot (`HaltServer 'Worker failed to boot.'`). Fixed by adding
  `COPY scripts/ ./scripts/`. Confirmed no `__init__.py` needed — Python 3.3+ implicit namespace
  packages (PEP 420) handle a directory without one, since the fix was just making the directory
  present in the image at all.
- **Design walked through one question at a time (Redis's own "Distributed locks with Redis" docs
  read directly, then applied)**, landing on:
  - **Separate lock key** (`fullscan:lock:limit:{limit}`) distinct from the data key
    (`fullscan:limit:{limit}`) — reasoned explicitly that conflating the two would let a lock
    placeholder be misread as real cached data by an unrelated `GET`.
  - **`SET lock_key token NX EX <ttl>`** as the acquire mechanism — `NX` as the mutex (only one
    concurrent `SET` can succeed), a per-request random `token` (`str(uuid.uuid4())`, correctly
    reasoned as sufficient — not cryptographic security, just "unique enough," matching the Redis
    doc's own framing) stored as a local variable scoped to that request's coroutine (no shared
    state needed, since each HTTP request is its own independent invocation).
  - **Safe compare-and-delete release via Lua script** (`scripts/deleteKey.py`,
    `if redis.call("get",KEYS[1]) == ARGV[1] then return redis.call("del",KEYS[1]) else return 0
    end`), executed atomically through `redis_client.register_script(...)` (registered once in
    `lifespan`, called per-release) — **not** a plain blind `DELETE`. Reasoned through the exact
    failure mode this avoids: if a slow winner's lock outlives its own `EX` and a second request
    acquires a fresh lock in the gap, the original (slow) winner finally finishing and blindly
    deleting would delete the *second* request's legitimate lock, letting a third request pile on —
    a cascading correctness bug a single unconditional `DELETE` doesn't protect against. Note:
    Redis's newer `DELEX key IFEQ` (built-in equivalent, Redis 8.4+) isn't available on `redis:7-
    alpine`, confirmed via version check before committing to the Lua-script route.
  - **Losers poll the *data* key** (not the lock key) at a fixed 500ms interval, up to N retries,
    then fall back to computing themselves unprotected if the budget is exhausted — deliberately
    chosen over Pub/Sub or `BLPOP`-based notification (both discussed and explicitly deferred as a
    "production-grade upgrade" to revisit later, not implemented this round) since the goal was
    demonstrating the coalescing *concept* clearly, not building a fully production-grade version
    first. **User asked to be reminded to circle back to Pub/Sub and `BLPOP` as the more advanced,
    non-polling approaches — still outstanding.**
- **Two real `await`-missing bugs caught via the same trace-by-hand method as prior sessions**: first
  `fetchFromDb(...)` (an `async def` helper) called without `await` at both call sites, and
  `app.state.lockDelSript(keys=[...], args=[...])` (the registered Lua script call, also async on an
  async Redis client) called without `await` — both silently returned unresolved coroutine/Script-call
  objects instead of real values until fixed. Consistent recurring bug class across this whole session
  (same root cause as the original `redis.get`/`redis.set` await bugs from 2026-09-19).
- **Lock TTL vs. wait-budget tuning went through two iterations, each empirically tested rather than
  guessed once and trusted:**
  - **Iteration 1**: `EX=18` (just under the 20s data TTL, chosen as a crash-safety margin) / wait
    budget `range(20)` × 500ms = 10s max. **Result**: real improvement over the unprotected stampede
    but still degraded — two consecutive 10-VU runs showed 11.36% then 0% failed, avg 21.98s then
    11.38s. **Root cause correctly self-diagnosed by the user** (one assist needed): request
    completion time under 10-VU contention (up to ~42s observed) regularly *exceeds* the 18s lock
    TTL, so the lock can expire mid-computation, letting a second request acquire a fresh lock and
    start a smaller-scale duplicate stampede while the original winner is still legitimately working
    — not crashed, just slow. Also correctly identified the second half of the same problem
    unprompted: even if the lock TTL were raised, a waiter that gives up after only 10s of polling
    would *still* fall through and duplicate work against a winner that's merely slow, not dead — so
    the wait-budget needed to scale together with the TTL, not just the TTL alone.
  - **Iteration 2**: `EX=45` (comfortable margin above the observed ~42s worst case) / wait budget
    `range(80)` × 500ms = 40s max. **Result: 0% failed on both of two consecutive 10-VU runs**, avg
    duration **~6.8s** (down from 21.98s / 41.8s / 30.27s across the prior uncached/stampede/under-
    tuned-lock states), memory capped at **~1.8GB** (down from 3.35GB with the shorter TTL, and from
    the original 4.38GB OOM-triggering spikes with no protection at all). Total completed iterations
    nearly doubled (135–141 vs. 44–82 in the tighter-window runs) — substantially more useful work
    completed in the same wall-clock window, not just fewer failures.
- **Full before/after comparison table, 10 VUs, weighted limits, 90s runs**:
  | Scenario | Failed | avg | median | peak `feed_api` mem |
  |---|---|---|---|---|
  | Uncached | 38.70% | 30.27s | 28.1s | 4.38GB (OOM) |
  | Cached, no protection (stampede) | 66.66% | 41.8s | 47.23s | OOM |
  | Cached, lock (18s TTL / 10s wait) | 0–11% | 12–22s | 10–14s | 3.35GB |
  | Cached, lock (45s TTL / 40s wait) | **0.00%** | **~6.8s** | **~4.1s** | **~1.8GB** |
  | (reference) 2 VUs, cached, lock | 0.00% | 2.87s | 0.91s | <1GB |
- **Remaining gap to the 2-VU numbers correctly reasoned through as structural, not a bug**: at 10
  VUs spread across 5 `limit` keys with `800000` dominant (88–100 of ~135–141 total requests per
  run), only one request per *key* can be the active "winner" computing at a time even with a
  correctly working lock — so real queuing on the hottest key is expected and legitimate, not a sign
  the protection is incomplete.
- **This closes out the stampede-protection experiment and the plan's Phase 2 milestone honestly**:
  a complete, self-demonstrated three-way before/after story (uncached → cached-without-protection →
  cached-with-protection), each transition backed by real measured numbers and a root-cause
  explanation, not assumptions.
- **Next session: pick up here.**
  1. **Outstanding reminder (user-requested)**: revisit Pub/Sub and `BLPOP`-based waiter notification
     as the more production-correct alternative to fixed-interval polling — deferred, not forgotten.
  2. Decide whether to add a memory cap to the `redis` service now that real cache sizes/usage
     patterns are known from these runs (still deferred from 2026-09-19).
  3. Minor cleanup flagged earlier but not yet done: duplicated fetch/serialize/cache logic between
     the lock-acquired and fallback-after-retries branches in `getFullScanFeed` could be extracted
     into a shared helper (partially done via `fetchFromDb`, but the caching/response-building steps
     around it are still duplicated).
  4. Decide next phase per `live_feed_engine_plan.md`: Phase 3 (Kafka/outbox), which the user has
     specifically said they want to own.

### 2026-09-21 — outstanding lock-design upgrades, deferred to a dedicated future session
Two separate, orthogonal upgrades to the stampede-protection lock were identified during design
discussion but deliberately not implemented this round (simpler options chosen instead, to keep the
first working version learnable/buildable in one session). Both remain real, well-understood next
steps — not vague ideas, concrete designs already reasoned through:

1. **Lock lease renewal (heartbeat), replacing the current fixed TTL.**
   - **Problem it solves**: the current lock (`SET fullscan:lock:limit:{limit} token NX EX 45`) commits
     to one fixed TTL *at acquisition time*, before the winner knows how long its own work will
     actually take. Guess too short and the lock can expire mid-computation while the winner is still
     alive and working (the exact bug diagnosed and fixed this session by raising `EX` from 18→45 and
     the wait budget from 10s→40s to match observed worst-case completion times, ~42s). Guess too long
     and a genuinely crashed winner's lock lingers unnecessarily, slowing recovery for everyone else.
   - **The fix**: instead of one static `EX` set once, the winning request periodically "renews"
     (extends) the lock's TTL at an interval shorter than the TTL itself, *while it's still actively
     working* — e.g. extend by another N seconds every N/2 seconds — and simply stops renewing (letting
     it expire naturally) once the work finishes or the process dies. The lock then lives almost exactly
     as long as real work is happening, no more, no less — removing the need to guess a single worst-case
     number up front.
   - **Why deferred**: meaningfully more complex than a fixed TTL — needs a background task (or periodic
     check interleaved with the main DB fetch) running alongside the actual computation, correctly
     coordinated with the existing token-based compare-and-delete release so a renewal never resurrects
     a lock a *different* winner has since taken over. Real crash risk (the main reason a TTL safety net
     exists at all) is also low on this single local Docker Compose setup, which is why the simpler fixed-
     TTL approach was judged acceptable for this first working version.
   - **This is orthogonal to item 2 below** — it's about how long the lock lives, not about how waiters
     find out when work is done. The two can be combined independently (see matrix in the 2026-09-21
     "fixed TTL vs. heartbeat, and how it relates to Pub/Sub/BLPOP" discussion).

2. **Pub/Sub or `BLPOP`-based waiter notification, replacing fixed-interval polling.**
   - **Problem it solves**: the current "loser" path (`for i in range(80): await asyncio.sleep(0.5); ...`)
     polls the data key every 500ms — simple, but wastes GET calls when nothing has changed yet, and
     introduces up to 500ms of pure latency between the winner actually finishing and a waiter noticing.
   - **The fix (two documented options)**:
     - **Redis Pub/Sub**: the winner, once done, `PUBLISH`es on a channel (e.g.
       `fullscan:done:limit:{limit}`); waiters `SUBSCRIBE` and block until notified instead of polling
       at all. Real complexity to handle: the classic race where a `PUBLISH` fires *before* a late
       subscriber has started listening (a subscriber that starts too late can miss the notification
       entirely and needs a fallback), plus subscriber lifecycle/cleanup via `redis-py`'s async pub/sub
       API.
     - **`BLPOP`/`BRPOP`**: a lighter-weight alternative — winner pushes a value onto a list once done,
       waiters block on `BLPOP` until something appears. Avoids the pub/sub "message published before
       anyone's listening" race (list items persist until popped, unlike pub/sub messages), simpler to
       reason about, still avoids polling.
   - **Why deferred**: the goal this session was demonstrating the *coalescing concept* clearly with a
     working, understandable implementation — polling is simple to trace and reason about by hand, which
     matched the project's learning-mode goals better as a first pass. Pub/Sub/`BLPOP` are real
     production-grade upgrades explicitly flagged by the user as "come back to this," not rejected as
     wrong approaches.
   - **User's own framing worth preserving verbatim for next session**: distinguished these as solving
     "how do waiters find out" vs. lease renewal solving "how long does the lock live" — confirmed
     orthogonal, not competing solutions, can be combined in any pairing.

**Suggested order for the dedicated follow-up session**: implement heartbeat/lease renewal first (smaller,
self-contained change to the existing lock-acquisition code path), verify it under the same 10-VU weighted
k6 scenario (expect the lock to survive computations of any length without needing a hand-tuned worst-case
`EX` guess), *then* tackle Pub/Sub or `BLPOP` as the waiter-side upgrade, since it's the larger structural
change (new message-passing pathway, not just a modified `SET`/renew call) and benefits from being tested
against an already-correct lock-lifetime implementation rather than debugging both changes at once.

### 2026-09-16/17 — `seed.py` rewritten for chunked `COPY` (islice-based batching)
- **Motivation**: previous seeding path wasn't chunked — this session rebuilt `batch_generator` to
  slice a row-generator into fixed-size batches and `COPY` one chunk at a time, needed for the
  10M–50M-row bump identified above (Phase 2 prerequisite).
- **Built via Socratic back-and-forth (user wrote every line by hand), landed on**:
  `generate_rows(n, publisher_ids)` — unchanged generator (`yield`) producing one row tuple at a
  time. `batch_generator(rows, batch_size)` — `while batch := list(islice(rows, batch_size)): yield
  batch`, slicing the generator object directly (no `iter()` needed — a generator object is already
  an iterator). `main()` loops `for tuples in batch_generator(...): for row in tuples:
  writer.writerow(row)`, then one `buf.seek(0)` + `cur.copy_expert(...)` + `buf.seek(0)` +
  `buf.truncate(0)` **per chunk**, not per row.
- **Bugs caught and fixed along the way** (all by the user, self-diagnosed after Socratic
  questioning, not given as code):
  1. `iter(count)` called on an int (`count` was the row-count int, not the generator) — fixed by
     passing the actual generator object returned from `generate_rows(...)`.
  2. Outer loop variable (`for row in batch`) was actually a whole chunk (list of ~1M tuples), not
     one row — fixed by adding the inner `for row in tuples` loop before `writer.writerow`.
  3. `buf.seek(0)`/`truncate(0)` were initially placed inside the per-row loop, collapsing batching
     back into one `COPY` per row — fixed by moving the seek/copy/truncate block to run once per
     chunk, after all rows of that chunk are written.
- **Dry run (`main(5)`) hit a real constraint, not a code bug**: `psycopg2.errors.UniqueViolation`
  on `uq_url` — expected, since ~10M rows already exist in `article` from earlier seeding/polling
  sessions and `article/0`, `article/1`, etc. collide. Confirmed the chunking/COPY mechanics
  themselves worked (reached `copy_expert` and Postgres processed the batch before rejecting it).
  **Resolution: truncate/delete `article` before a clean reseed** (chosen over unique-offset URLs or
  a staging-table + `ON CONFLICT DO NOTHING` approach, since this is disposable synthetic seed data).
- **Also learned**: `conn.commit()` only fires once, after the full chunk loop — so row count stays
  visibly frozen in another `psql` session for the entire run, not because nothing is happening, but
  because Postgres only shows committed data to other sessions (default READ COMMITTED isolation).
- Ran the real seed (`main(50_000_000)`, default) after the above — **result not yet confirmed in
  this log; check `SELECT count(*) FROM article;` next session** once it's had time to finish and
  commit.
- Visual summary saved to Obsidian:
  `Learning/Backend Engineering/Python/FastAPI/8-chunked-seeding-islice.html` (covers
  `itertools.islice`, generator-object-as-iterator, the walrus `while` batching loop, and
  `StringIO.seek`/`.truncate` semantics).
