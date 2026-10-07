# mosaic-mcp

<!-- mcp-name: io.github.sourabhnk/mosaic-mcp -->

Pre-clinical drug-target intelligence as an MCP server: target profiles,
compounds, literature, structure, clinical pipeline, validation precedent and
whitespace analysis — 11 tools, 5 free and 6 Pro. Every count carries a
coverage state, so "we did not look" never renders as zero.

## Which one do you want?

**This package is bring-your-own-database.** It is the MCP tool layer only —
it ships the queries, not the data. You point it at a PostgreSQL instance you
control, with the Mosaic schema, and it serves the tools over it. There is no
public read-only credential for Mosaic's knowledge graph.

**If you want Mosaic's curated knowledge graph** — 60 oncology targets,
~128K compounds linked to them, 57,896 papers and 9,062 clinical trials
(counted 2026-10-07), refreshed monthly — use the **hosted server**.
It needs no database and no local install:

- Claude.ai connector (OAuth, Streamable HTTP): `https://mcp.getmosaic.dev/mcp`
- Remote MCP with an API key (SSE): `https://mcp.getmosaic.dev/sse`
- Sign in, API keys and on-demand dossiers for any human gene: <https://getmosaic.dev>

## Quick start (self-hosted)

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
mosaic-mcp                               # stdio transport
```

`uvx mosaic-mcp` and `python -m mosaic_mcp.server` work too. This package is
**stdio-only**: `--transport sse` exits with `NotImplementedError`, because
remote transport is the hosted server above.

## Tools

### Free (5)

| Tool | What it returns |
|------|-----------------|
| `mosaic_get_target_profile` | The target dossier: biology, approved and clinical drugs, disease associations ranked by Open Targets, validation evidence, organisations, scores — each axis with its coverage state |
| `mosaic_get_target_compounds` | Compounds with measured activity on the target; `sort` by potency (default) or by max clinical phase |
| `mosaic_get_target_papers` | Papers about the target, matched by NCBI Gene ID (PubTator3) where your database has the annotations, up to the declared corpus cutoff |
| `mosaic_get_target_structure` | AlphaFold structural snapshot and pockets, with a caution when a pocket is lined by low-confidence residues |
| `mosaic_get_coverage_grid` | A coverage grid for any human gene symbol: identity (HGNC) and structure (AlphaFold DB) resolve live; the other axes are marked `queued` |

Free calls return up to 10 compounds or papers; Pro up to 50.

### Pro (6)

| Tool | What it returns |
|------|-----------------|
| `mosaic_clinical_pipeline` | Clinical trials for compounds acting on the target (a ChEMBL mechanism link or pChEMBL ≥ 6) |
| `mosaic_target_validation` | Assay precedent: what has been tried, in what model system |
| `mosaic_pathway_context` | The pathways the target sits in |
| `mosaic_target_network` | The target's 1-hop neighbourhood: compounds, diseases, pathways, organisations, interacting proteins |
| `mosaic_synthetic_lethal_whitespace` | Partners functionally coupled to the target with little drug activity — assessed candidates and unassessed partners kept apart |
| `mosaic_resistance_bypass_map` | Candidate resistance-bypass and escape targets |

`mosaic_request_dossier` and `mosaic_get_dossier` (on-demand dossiers) run only
on the hosted server; they need its fetch and synthesis pipeline.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string for your own instance; the path must name the database that holds the Mosaic schema. |
| `MOSAIC_TIER` | No | `pro` (or `enterprise`) enables the Pro tools in a self-hosted install. |
| `MOSAIC_API_KEY` | No | In stdio mode any value is treated as Pro; it is not checked locally. Keys are validated by the hosted server. |

## Changelog

Full history in [CHANGELOG.md](CHANGELOG.md). Latest: **2.1.1** — the
`mosaic-mcp` command starts again (it had failed with `ImportError` since
1.7.0). **2.1.0** capped `mcp` below 2, without which a fresh install died on
import, and carried the fixes from a live audit of the hosted server.

## License

Apache 2.0 for the MCP server tools. The hosted knowledge graph data requires a subscription for Pro-tier access.
