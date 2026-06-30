"""Mosaic MCP — Pre-clinical drug discovery intelligence server.

44 MCP tools for targets, compounds, patents, clinical trials,
competitive landscapes, whitespace analysis, and more. Powered by a
PostgreSQL knowledge graph.

Usage:
    pip install mosaic-mcp
    export DATABASE_URL="postgresql://..."
    mosaic-mcp                               # starts stdio MCP server
    mosaic-mcp --transport sse --port 3001   # SSE for remote clients
"""

__version__ = "1.1.0"
