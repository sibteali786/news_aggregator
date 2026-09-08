# Live Feed Engine — Build Plan
Real news ingestion + synthetic load → Postgres → Redis → Kafka, built in pain-driven phases.

**Ownership split:**
- 🔧 **You build by hand:** Kafka, Redis, Postgres setup/config, partition keys, CDC/outbox logic, consumer group behavior — anything that IS the learning.
- 🤖 **Delegate to Claude Code:** REST API boilerplate, frontend UI, Docker Compose scaffolding, repetitive CRUD, chart components.

**Data source:** Real RSS feeds (BBC, Reuters, HN, TechCrunch — all free, no auth) for organic ingestion, PLUS a synthetic seeder script for on-demand load/hot-key demos you can trigger at will.

---

## Phase 0 — Foundations (you)
- Docker Compose: Postgres only (Kafka/Redis added later, don't pre-install)
- Schema: `Article(id, title, url, publisher, category, region, published_at)`, `Publisher(id, name, feedUrl, region)`
- Write a tiny RSS poller (Python `feedparser`) that inserts into Postgres every N minutes — this is your "Data Collection Service" from the design doc
- **Delegate to Claude Code:** a minimal `GET /feed?region=&limit=&cursor=` REST API over Postgres directly (no cache yet)

**Milestone:** working feed, zero caching, zero streaming. This is your honest v1 — same as the Hello Interview HLD before any deep dive.

---

## Phase 1 — Feel the pain (you + Claude Code for the load tool)
Don't add Redis speculatively — prove you need it first, on camera/content-wise this is great material too.

- Synthetic seeder: generate 100k–1M fake articles into Postgres (Claude Code can help write this fast)
- Load test the `/feed` endpoint (`k6` or `wrk`) at increasing concurrency
- Watch: query latency climb, `EXPLAIN ANALYZE` showing full/expensive scans as offset pagination gets deep, connection pool exhaustion under concurrency
- **Document the exact breaking point** (e.g. "p99 latency crosses 200ms at ~800 concurrent users, 500k rows") — this number is your content hook and your interview talking point

**Milestone:** a screenshot/graph of Postgres degrading. This IS the deep-dive trigger, same as in the Hello Interview framework — you're re-deriving "why do I even need a cache" from first principles instead of assuming it.

---

## Phase 2 — Add Redis (you)
- Redis sorted sets per region: `ZADD feed:US <timestamp> <articleId>`
- API now reads Redis first, falls back to Postgres on miss
- Re-run the same load test → compare graph side by side with Phase 1
- **Demo-able concept:** cache TTL + thundering herd — intentionally set a short TTL, hammer it, watch requests pile onto Postgres at expiry. Fix it, show the fix.

**Milestone:** two load-test graphs (before/after Redis) — this pairing is your strongest single piece of content.

---

## Phase 3 — Introduce Kafka, hand-rolled outbox first (you)
This is the part you specifically want to own — good call, this is where the real understanding lives.

- Add an `outbox_events` table; every Postgres write also inserts an outbox row in the same transaction
- Write your own **outbox poller**: polls `outbox_events WHERE processed=false`, publishes to Kafka, marks processed
- Kafka topic: `articles.created`, partitioned by `region` (or `region:category` compound key — this is your good/bad key demo)
- Feed Worker (consumer): reads topic, does `ZADD` into Redis — this replaces "write API updates Redis directly," decoupling ingestion from cache-serving (same separation of concerns as your design doc)

**Concepts to explicitly demo here:**
- Bad key (`category` only — 5 values, imbalanced) vs good key (`region:category`, spread evenly) — show partition distribution both ways
- Kill a Feed Worker mid-stream, show consumer group rebalance + lag recovery
- Turn off `enable.idempotence`, force a producer retry (simulate network blip), show duplicate Redis entries; turn it on, show it's clean

**Milestone:** hand-built CDC-via-outbox is working end to end, and you can articulate every step because you wrote it.

---

## Phase 4 — Add Debezium CDC alongside (you)
Your idea to keep both is good — it's a genuine comparison piece, not redundant.

- Stand up Debezium (Postgres → Kafka Connect) pointed at the *same* Postgres tables, but publishing to a parallel topic (`articles.created.debezium`)
- Run outbox-poller and Debezium side by side, same write load
- Compare: latency from write to Kafka message, exactly-once vs at-least-once behavior, operational complexity (config vs code), what happens on schema changes

**Content angle:** "I built CDC by hand, then swapped in Debezium — here's what changed and why teams use Debezium in production despite the extra infra." That's a legitimately rare, senior-sounding comparison to have actually done.

---

## Phase 5 — CLI wrapper (you, this ties Phases 1–4 together)
Subcommands, each mapped to a concept:
```
feedcli seed --count 100000
feedcli benchmark --endpoint /feed --concurrency 500
feedcli produce --key-strategy bad|good
feedcli show-partitions
feedcli show-lag --group feed-workers
feedcli kill-consumer --id 2
feedcli toggle-idempotence --on|off
feedcli compare-cdc --writes 1000
```

---

## Phase 6 — Web UI (delegate to Claude Code)
Give Claude Code the CLI's underlying functions as an API layer, then have it build:
- Live partition-distribution bar chart (updates on produce)
- Consumer lag line graph over time
- Toggle switches for "bad key / good key," "idempotence on/off," "TTL cache / CDC cache" — each toggle re-runs a benchmark and shows the diff live
- A big red "kill this consumer" / "simulate publisher spike" button — this is the shareable, demo-able moment for your content

---

## Why this ordering matters (for your resume story)
The narrative isn't "I used Kafka" — it's **"I identified exactly where Postgres alone breaks, added Redis to fix that, then identified where direct-write-to-cache breaks under bursty load, and added Kafka to decouple ingestion from serving — with measured numbers at each step."** That's the sentence that survives a skeptical interviewer follow-up, because you can back every claim with a graph you actually produced.

---

## Immediate next step
Start Phase 0 today: Postgres + schema + RSS poller + Claude-Code-built basic `/feed` endpoint. Say the word and I'll hand you the Docker Compose + schema to kick it off, or you can take this plan straight into Claude Code.
