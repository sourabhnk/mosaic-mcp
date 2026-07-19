"""Mosaic MCP — Pre-clinical drug discovery intelligence server.

44 MCP tools for targets, compounds, patents, clinical trials,
competitive landscapes, whitespace analysis, and more. Powered by a
PostgreSQL knowledge graph.

Usage:
    pip install mosaic-mcp
    export DATABASE_URL="postgresql://..."   # bring your own Postgres
    mosaic-mcp                               # starts stdio MCP server

This package is stdio-only. `--transport sse` raises NotImplementedError —
remote access is served by the hosted endpoint (mcp.getmosaic.dev), not by
this package. The flag was documented here as working; it never was.
"""

__version__ = "1.2.0"
