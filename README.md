# mosaic-mcp

<!-- mcp-name: io.github.sourabhnk/mosaic-mcp -->

Pre-clinical drug discovery intelligence as an MCP server. Query 760+ drug targets, 70K+ compounds, 48K+ papers, 18K+ clinical trials, and 16K+ patents through 44 specialized tools — 16 free for discovery, 28 Pro for competitive landscapes, whitespace, and thesis-grade analysis.

## Which one do you want?

**This package is bring-your-own-database.** It is the MCP tool layer only —
it ships the queries, not the data. You point it at a PostgreSQL instance you
control and it serves 44 tools over it. There is no public read-only
credential for Mosaic's knowledge graph, and earlier versions of these docs
implied otherwise.

**If you want Mosaic's actual curated KG** — 760+ targets, 70K+ compounds,
48K+ papers, 18K+ trials, 16K+ patents, kept current by a monthly refresh —
use the **hosted server** instead. It needs no database and no local install:

- Remote MCP: `https://mcp.getmosaic.dev/sse`
- Sign in / API keys: <https://getmosaic.dev>

## Quick Start (self-hosted)

```bash
pip install mosaic-mcp
```

### With Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mosaic": {
      "command": "mosaic-mcp",
      "env": {
        "DATABASE_URL": "postgresql://USER:PASSWORD@YOUR-HOST:5432/mosaic_db?sslmode=require"
      }
    }
  }
}
```

### With Claude Code

```bash
claude mcp add mosaic -- mosaic-mcp
```

### Standalone

```bash
export DATABASE_URL="postgresql://..."   # your own Postgres
export MOSAIC_API_KEY="msk_..."          # Optional — for Pro tools
mosaic-mcp                               # stdio transport
```

This package is **stdio-only**. `--transport sse` exits with
`NotImplementedError`; remote transport is served by the hosted endpoint
above, not by this package. The flag was previously documented as working.

## Tools

### Free Tier (16 tools) — discovery + workspace

| Tool | Description |
|------|-------------|
| `mosaic_search_targets` | Search drug targets by name, gene symbol, or keyword |
| `mosaic_get_target_profile` | Comprehensive target dossier (biology, compounds, scores) |
| `mosaic_get_target_compounds` | Compounds tested against a target with SAR data |
| `mosaic_get_target_patents` | Patent landscape for a target |
| `mosaic_get_target_papers` | Literature for a target |
| `mosaic_get_target_structure` | 3D structure and ligandability summary for a target |
| `mosaic_kg_stats` | Knowledge graph overview statistics |
| `mosaic_list_indications` | Available disease indications |
| `mosaic_list_subindications` | Sub-indications within an indication area |
| `mosaic_subindication_breakdown` | Per-target activity across sub-indications |
| `mosaic_target_scores` | Target attractiveness scoring |
| `mosaic_target_wishlist_add` | Add a target to your personal wishlist |
| `mosaic_watchlist_create` | Create a watchlist |
| `mosaic_watchlist_add_item` | Add an item to a watchlist |
| `mosaic_watchlist_get` | Retrieve a watchlist |
| `mosaic_watchlist_list` | List your watchlists |

### Pro Tier (28 additional tools) — analysis + whitespace

The committed-verdict layer. Includes the whitespace and differentiation tools:
`mosaic_synthetic_lethal_whitespace`, `mosaic_modality_gaps`,
`mosaic_resistance_bypass_map`, `mosaic_find_undruggable_targets`,
`mosaic_talent_migration`, `mosaic_emerging_signals`, and
`mosaic_assess_druggability`.

Plus the full analysis set: competitive landscape, pathway context, compound
selectivity, indication landscape, target validation, clinical pipeline,
compound analogs, target comparison, similar-target search, opportunity
finding, organization portfolio, target network, mechanism of action, evidence
maps, relation search, polypharmacology, clinical trial results, FDA regulatory
status, drug comparison, drug repurposing candidates, and KOL discovery.

## Data Coverage

| Entity | Count |
|--------|-------|
| Drug Targets | 764 (oncology + neuroscience + cardiovascular) |
| Compounds | 71,512 |
| Clinical Trials | 18,580 |
| Papers | 48,773 |
| Patents | 16,189 |
| Semantic Relations | 13,704 |
| Indications | 24,949 |
| Organizations | 36,691 |

<sub>Counts as of 2026-07-18. The live figures are always
`mosaic_kg_stats`; run it rather than trusting this table. Targets went 802 →
764 when duplicate and malformed rows were merged, and organizations
153,852 → 36,691 when affiliation parsing was corrected — both are the count
getting more honest, not the corpus shrinking.</sub>

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string — your own instance |
| `MOSAIC_API_KEY` | No | API key for Pro tool access |
| `MOSAIC_TIER` | No | Override tier (`free`, `pro`, `enterprise`) |

## Changelog

### 1.2.0 — breaking change to the watchlist tools

**`owner_key` must now be `anon:<token>`.** It was previously free text
documented as "user id, email, or `anon:<token>`", which meant one caller
could name another and read their lists. A bare id or email is now refused.

Generate a random token, keep it, and pass the same one every time — it is
what proves a list is yours. Calls that pass an email or bare id now fail
with a message telling you this; they do not silently return someone else's
data, which is what the old behaviour risked.

Also in 1.2.0:

- Watchlist reads are owner-scoped **in the SQL**, so a watchlist UUID is no
  longer sufficient for read or write access, and they are excluded from the
  response cache (a cache hit would otherwise return before the ownership
  check ran).
- `mosaic_synthetic_lethal_whitespace` now returns `candidates` and
  `coupled_unassessed` as two separate lists. Partners outside the curated
  universe carry null counts instead of zeros — an unmeasured competitor
  count previously read as "no competition."
- Project metadata now points at `getmosaic.dev`. 1.1.0 and earlier pointed
  at a domain that is not ours.
- Query/response layer resynced to the hosted server. This includes the
  protein-protein-interaction reader fix: 1.1.0 counted PPIs from the legacy
  `target_interactions` table, while the hosted server had already moved to
  the populated `target_interactions_ext` table. Driven against hosted on
  2026-07-19, EGFR reports 50 interactions at confidence 0.999; the 1.1.0
  reader does not read that table for the profile count.

## License

Apache 2.0 for the MCP server tools. The hosted knowledge graph data requires a subscription for Pro-tier access.
