# mosaic-mcp

<!-- mcp-name: io.github.sourabhnk/mosaic-mcp -->

Coverage-honest pre-clinical target intelligence over MCP. Every count carries
a coverage state, so "we did not look" never reads as zero.

**3.0.0 is a thin client to the hosted Mosaic service.** It serves Mosaic's
five MCP tools over stdio and forwards every call to
`https://mcp.getmosaic.dev/mcp` with your API key. It holds no database and no
query layer: the answers, and any refusal, are the hosted service's.

> **Deprecated: the 2.x bring-your-own-database mode.** Versions up to 2.2.1
> ran Mosaic's query layer over a PostgreSQL database you provided
> (`DATABASE_URL`). That mode is deprecated and is not in 3.0.0; `DATABASE_URL`
> is ignored. Its eleven tools answer an old client by name with what replaced
> them. To keep running it, pin `mosaic-mcp<3` (2.2.1 is the last release).

## Do you need this package?

If your MCP client can connect to a remote server, you do not: add
`https://mcp.getmosaic.dev/mcp` as a connector (in claude.ai: Settings →
Connectors → Add custom connector). It signs you in by email (OAuth) and needs
no key and no install.

This package is for clients that only run local (stdio) servers.

## Quick start

```bash
pip install mosaic-mcp          # or: uvx mosaic-mcp
export MOSAIC_API_KEY="mk_live_..."
mosaic-mcp                      # stdio MCP server
```

### With Claude Desktop

```json
{
  "mcpServers": {
    "mosaic": {
      "command": "uvx",
      "args": ["mosaic-mcp"],
      "env": { "MOSAIC_API_KEY": "mk_live_..." }
    }
  }
}
```

### With Claude Code

```bash
claude mcp add mosaic --env MOSAIC_API_KEY=mk_live_... -- uvx mosaic-mcp
```

## Tools

| Tool | Tier | What it does |
|------|------|--------------|
| `mosaic_get_coverage_grid` | free | A coverage grid for any human gene symbol: every axis with its state; network axes `queued` until a dossier fetches them |
| `mosaic_verify_claim` | free, rate-limited | One claim about a target checked against Mosaic's own independent count, in at most 10 s, with one of seven verdicts (`confirmed`, `contradicted`, `cannot_verify_same_source`, `cannot_verify_floor`, `cannot_verify_unavailable`, `cannot_verify_unscoped`, `out_of_scope`) |
| `mosaic_audit_claims` | paid ($390 an audit) | Every target-level claim in a document (a press release, a filing section, an AI-generated assessment), audited; async |
| `mosaic_request_dossier` | paid ($149 a dossier) | A verified dossier for any human target; async |
| `mosaic_get_result` | free | The status and result of a dossier (`dossier-N`) or audit (`audit-N`) you requested; only your own |

The tier, the price and any refusal come from the hosted service; this package
decides none of them.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `MOSAIC_API_KEY` | Yes | Your Mosaic API key (`mk_live_…`), sent as a Bearer token. Without it every call says so and sends nothing. |
| `MOSAIC_MCP_URL` | No | The hosted MCP endpoint. Default `https://mcp.getmosaic.dev/mcp`. |

A call the hosted service could not answer (unreachable, a timeout, a rejected
key) is returned as an error saying so: never as an empty or a zero result.

## Changelog

Full history in [CHANGELOG.md](CHANGELOG.md). **3.0.0**: a thin client to the
hosted service with the five tools of plan v3 §3.1; the bring-your-own-database
mode and its eleven tools are retired (each answers with its replacement).

## License

Apache 2.0.
