"""Mosaic MCP: a thin client to the hosted Mosaic service.

Five MCP tools over stdio (the coverage grid, verify_claim, the claims audit,
the dossier request, get_result), each call forwarded to
https://mcp.getmosaic.dev/mcp with your API key.

Usage:
    pip install mosaic-mcp
    export MOSAIC_API_KEY="mk_live_..."
    mosaic-mcp                               # stdio MCP server

3.0.0 is a deliberate break: the 2.x bring-your-own-database mode is
deprecated and not in this package. Pin `mosaic-mcp<3` to keep it.
"""

__version__ = "3.0.0"
