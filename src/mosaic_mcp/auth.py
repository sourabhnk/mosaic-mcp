"""Stub auth for the standalone stdio package (no remote SSE transport)."""

def principal_tier():
    """No per-request principal in stdio mode; tier comes from env."""
    return None


def build_authenticated_sse_app(mcp):
    """SSE transport is served by the hosted backend, not the pip package."""
    raise NotImplementedError(
        "The mosaic-mcp pip package runs stdio only. "
        "Use the hosted endpoint for remote SSE access."
    )
