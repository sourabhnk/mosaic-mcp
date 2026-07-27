# Changelog

All notable user-facing changes to the `mosaic-mcp` package. This package
bundles Mosaic's knowledge-graph query and rendering layer; it is
bring-your-own-database. Dates are UTC.

## 1.6.0 — 2026-07-27

Adds one tool: an orientation front door. No behaviour changes to the existing 44.

### `mosaic_start_here` — call it first

- Returns the full capability map — all 44 tools in 8 groups, each with what it
  answers, a runnable example, and whether your tier can call it. The problem it
  solves: 44 tools with no entry point means a client picks one by name-matching
  and often picks wrong.
- **Not gated**, so a free session can always call it, and it needs no database
  of its own — if `DATABASE_URL` is unset or unreachable it still answers, with
  `_provenance.as_of` null. Orientation should not fail because setup is
  incomplete.
- It states plainly that **this package is bring-your-own-database**: it queries
  whatever `DATABASE_URL` points at, there is no shared Mosaic database to
  connect to, and every count reflects *your* database. If you wanted Mosaic's
  hosted knowledge graph, it points you at `https://mcp.getmosaic.dev/sse`.
- It does **not** report a monthly query quota or a daily target limit, because
  this package does not enforce them — it gates which *tools* you may call and
  meters nothing. Those limits belong to the hosted server. The payload says so
  rather than leaving you to assume either way.

`mosaic_start_here` is included in the free tier (now 17 tools of 45).

## 1.5.0 — 2026-07-26

A correctness release. Every change below removes a way the tools could return a
confident, well-formed answer that was wrong — the failure mode Mosaic exists to
prevent. If you are on 1.4.0 or earlier, upgrade: 1.4.0 shipped several of these
defects.

### Clinical trials now mean "developed against this target"
- A target's trial count is derived from trials of compounds that are *developed
  against* it — a curated mechanism of action, or potent activity (pchembl ≥ 6,
  ≤ 1 µM) — not from every drug that incidentally binds it. Previously a
  promiscuous off-target (e.g. a nicotinic receptor swept in by many CNS drugs)
  accumulated the trials of every drug that merely touched it.
- A trials count is never labelled "complete." It renders as `truncated` with the
  reading **"at least N"**, and the coverage block states *why* (an upstream
  compound search that is itself incomplete) rather than falsely claiming a
  per-target cap was hit.

### Coverage and scores never dress up an absence as a measurement
- **`data_coverage` is a floor, not "the true total per axis."** The per-axis
  count is labelled as current ingestion coverage; it never asserts it is the
  complete total.
- **An unmeasured score is `null`, never `0.0`.** A score that has not been
  measured is no longer rendered as a number a reader would mistake for "low."
  The denominator behind each score is stated.

### Momentum honesty
- Momentum measures the target's own trajectory, not the calendar, and reads the
  writer's own vocabulary instead of inventing a freshness date.

### Internal
- The agent-response cache keys on tier, so a higher-tier answer can never be
  served from cache to a lower tier.

## 1.4.0 — 2026-07-25
- Assay-precedent reframe: validation evidence is surfaced as ranked exemplar
  papers with a required trial cue, never a fabricated outcome verdict.
- Honest activity naming: a "best IC50" that is actually a Kd/Ki is named for the
  metric it is.
- Response-cache purge no longer reports success against an unreachable database.

## 1.3.0 — 2026-07-24
- Owner-scoped watchlists; the query layer resynced with production; honest
  install docs (bring-your-own-database).

Earlier versions predate this changelog; see the git history.
