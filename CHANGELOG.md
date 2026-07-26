# Changelog

All notable user-facing changes to the `mosaic-mcp` package. This package
bundles Mosaic's knowledge-graph query and rendering layer; it is
bring-your-own-database. Dates are UTC.

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
