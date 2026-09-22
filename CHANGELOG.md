# Changelog

## 2.0.0 — 2026-09-22

Positioning, not a defect (decision D1): patent intelligence leaves the
public surface.

- REMOVED: `mosaic_get_target_patents` and `mosaic_competitive_landscape`.
  The underlying data and query layer are unchanged; the tools are simply
  no longer part of the product surface.
- ADDED: `mosaic_get_coverage_grid` — a coverage-honest grid for ANY human
  gene symbol (free). Identity (HGNC) and structure (AlphaFold DB) resolve
  live; remaining axes are marked `queued` and are computed by the hosted
  dossier pipeline. Nothing unfetched is ever rendered as zero.
- On-demand dossiers (`mosaic_request_dossier` / `mosaic_get_dossier`) are
  hosted-only tools at getmosaic.dev — they require the fetch/synthesis
  queue and are not part of the self-hosted package.
- Compounds payloads carry the ChEMBL CC BY-SA 3.0 notice (ShareAlike).
- Pro price is now $79/mo.


All notable user-facing changes to the `mosaic-mcp` package. This package
bundles Mosaic's knowledge-graph query and rendering layer; it is
bring-your-own-database. Dates are UTC.

## 1.7.0 — 2026-08-10

Whitespace and resistance-bypass answers now say what they could not measure,
instead of reporting it as zero. One behaviour change worth reading before
upgrading.

### A partner with an approved drug is no longer "whitespace"

- `mosaic_synthetic_lethal_whitespace` judged a curated target on two tests
  (few patents AND no clinical compound) but a neighbour-tier gene on patents
  alone. Patent counts systematically under-report that tier — patents name
  proteins, not genes ("histamine H2 receptor", never HRH2) — so genes with
  approved drugs were surfacing as unexplored. Both tiers now get both tests.
- Each candidate carries `max_phase`. It was read internally for years by a
  crowding check and never written, so the clinical half of that check silently
  did nothing.

### ⚠️ `shared_pathways` and `validation_evidence` can now be `null`

- Both count tables keyed to the curated universe, so for a neighbour-tier gene
  they were structurally `0` — and published as if measured. They are now
  `null`, meaning "not measurable for this gene", and the accompanying
  `co_functionality_basis` reads `ppi_pathways_unassessed` rather than
  `ppi_only`.
- **If you parse these as integers, handle `null`.** A zero you cannot
  distinguish from an unknown is the thing this release exists to remove.

### `mosaic_resistance_bypass_map` says which zero

- Gains `zero_reason` and `zero_reason_counts`, matching the whitespace tool.
  Present on every response, `null` when candidates were returned.
- `zero_reason_counts.resistance_backed` vs `ppi_fallback_only` is the one to
  read: this tool falls back to protein-interaction partners when the
  resistance layer is empty for a target, so a non-empty answer does not by
  itself mean there is resistance evidence behind it.

### Counts that are floors now say so

- `compound_count_is_floor` marks a compound count that stopped at a scan
  budget. `true` means "at least N", and the caveat text says so in words too.

### Compatibility

- This package is bring-your-own-database, and this release queries columns
  that a schema built for 1.6.0 does not have. It probes for them: where they
  are absent the queries still run, neighbour-tier genes are reported as
  uncounted rather than assessed, and nothing is admitted on a measurement your
  database cannot supply. No migration is required to upgrade.

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
