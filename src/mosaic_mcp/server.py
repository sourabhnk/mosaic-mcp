"""mosaic-mcp 3.0: a thin MCP client to the hosted Mosaic service.

Mosaic's MCP surface is five tools (plan v3 §3.1, 2026-11-07):

    mosaic_get_coverage_grid   free   any human target, every axis with its state
    mosaic_verify_claim        free   one claim checked against Mosaic's own count, <= 10 s
    mosaic_audit_claims        paid   every claim in a document, as an audited report ($390)
    mosaic_request_dossier     paid   a verified dossier for any human target ($149)
    mosaic_get_result          free   the status and result of a request you made

This package serves those five over stdio and forwards every call to the hosted
service (https://mcp.getmosaic.dev/mcp), which answers, meters and decides what
a caller may do. It holds no database and no query layer, and it decides no
tier: the answer, or the refusal, is the hosted service's, passed through as
it came.

    MOSAIC_API_KEY   a Mosaic API key (mk_live_...), sent as a Bearer token
    MOSAIC_MCP_URL   optional; the hosted MCP endpoint (default above)

The 2.x mode, which ran Mosaic's query layer over a PostgreSQL database you
provided, is DEPRECATED and is not in 3.0. Its tools answer an old client with
what replaced them. To keep running it, pin `mosaic-mcp<3`.

The five tools' names, descriptions and input models are not written here:
`mosaic_mcp/tool_inputs.py` is a byte-copy of the hosted service's own
(`mosaic/src/mcp/tool_inputs.py`, kept in step by
`scripts/sync_pip_package.py`), so the two doors cannot describe one tool
differently or accept different arguments.
"""

# NOTE: do NOT add `from __future__ import annotations` here. The mcp SDK
# inspects raw `param.annotation` in Tool.from_function; PEP 563 stringized
# annotations break its argument model.

import argparse
import os
import sys
from datetime import timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mosaic_mcp import __version__
from mosaic_mcp.tool_inputs import (
    FIVE_TOOLS,
    RETIRED,
    TOOLS,
    AuditClaimsInput,
    CoverageGridInput,
    DossierRequestInput,
    ResultInput,
    VerifyClaimInput,
)

HOSTED_MCP_URL = "https://mcp.getmosaic.dev/mcp"
# The hosted tools bound themselves (the grid answers in seconds, verify_claim
# in at most 10 s, the async tools return a request id at once); this is the
# most the client waits for any one answer before saying it got none.
CALL_TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 30.0

NO_KEY = (
    "mosaic-mcp 3.0 forwards every call to the hosted Mosaic service, and MOSAIC_API_KEY "
    "is not set. Set it to your Mosaic API key (mk_live_...). Or connect your MCP client "
    "straight to https://mcp.getmosaic.dev/mcp, which signs you in by email (OAuth) and "
    "needs no key. Nothing was sent.")


def _endpoint() -> str:
    return (os.environ.get("MOSAIC_MCP_URL") or "").strip() or HOSTED_MCP_URL


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status inside a transport failure, if there is one (the MCP
    client raises from a task group, so it may sit inside an ExceptionGroup)."""
    seen: list[BaseException] = [exc]
    while seen:
        e = seen.pop()
        status = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        seen.extend(getattr(e, "exceptions", ()) or ())
        for nxt in (e.__cause__, e.__context__):
            if nxt is not None:
                seen.append(nxt)
    return None


async def _forward(name: str, params: Any) -> str:
    """Call `name` on the hosted service with these arguments and return its
    answer verbatim. A hosted refusal is raised as the hosted text; a failure
    to reach it is raised saying so. Never an empty or invented answer."""
    key = (os.environ.get("MOSAIC_API_KEY") or "").strip()
    if not key:
        raise ToolError(NO_KEY)
    url = _endpoint()
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    arguments = {"params": params.model_dump(mode="json", exclude_none=True)}
    headers = {"Authorization": f"Bearer {key}", "User-Agent": f"mosaic-mcp/{__version__}"}
    try:
        async with streamablehttp_client(url, headers=headers, timeout=CONNECT_TIMEOUT_S) as (read, write, _):
            async with ClientSession(read, write,
                                     read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_S)) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except ToolError:
        raise
    except BaseException as e:  # noqa: BLE001 - ExceptionGroup from the transport's task group
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        status = _http_status(e)
        if status in (401, 403):
            raise ToolError(
                f"The hosted Mosaic service refused MOSAIC_API_KEY (HTTP {status}): it is "
                "invalid or revoked. Nothing was answered.") from e
        why = f"HTTP {status}" if status else f"{type(e).__name__}: {e}"
        raise ToolError(
            f"mosaic-mcp could not get an answer from the hosted Mosaic service at {url} "
            f"({why}). This is a failed call, not a result; retry.") from e
    text = "".join(getattr(block, "text", "") for block in result.content or ())
    if result.isError:
        # The hosted SDK prefixes a tool's own error; this SDK adds the same
        # prefix again when the error is re-raised here, so it is said once.
        text = text.removeprefix(f"Error executing tool {name}: ")
        raise ToolError(text or f"the hosted Mosaic service refused {name}, without a reason")
    if not text:
        raise ToolError(f"the hosted Mosaic service answered {name} with no content; "
                        "this is a failed call, not a result")
    return text


def _deprecation_message(name: str) -> str:
    on, replacement = RETIRED[name]
    return (f"{name} was retired from Mosaic's MCP tools on {on}. mosaic-mcp "
            f"{__version__} serves the five that remain ({', '.join(FIVE_TOOLS)}), "
            f"forwarded to the hosted service. {replacement}. The 2.x package that served "
            f"it over your own database is deprecated; pin mosaic-mcp<3 to keep it.")


class _ThinMCP(FastMCP):
    """FastMCP that answers a retired tool, by name, with what replaced it,
    instead of "Unknown tool". The retired tools are not registered, so
    tools/list shows exactly the five."""

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if name in RETIRED:
            raise ToolError(_deprecation_message(name))
        return await super().call_tool(name, arguments)


mcp = _ThinMCP(
    "mosaic_mcp",
    instructions=(
        "Mosaic: coverage-honest pre-clinical target intelligence. Every count carries a "
        "state, so 'we did not look' never reads as zero. Answers come from the hosted "
        "Mosaic service."),
)


def _tool(name: str):
    spec = TOOLS[name]
    return mcp.tool(name=name, description=spec["description"], annotations=spec["annotations"])


@_tool("mosaic_get_coverage_grid")
async def mosaic_get_coverage_grid(params: CoverageGridInput) -> str:
    return await _forward("mosaic_get_coverage_grid", params)


@_tool("mosaic_verify_claim")
async def mosaic_verify_claim(params: VerifyClaimInput) -> str:
    return await _forward("mosaic_verify_claim", params)


@_tool("mosaic_audit_claims")
async def mosaic_audit_claims(params: AuditClaimsInput) -> str:
    return await _forward("mosaic_audit_claims", params)


@_tool("mosaic_request_dossier")
async def mosaic_request_dossier(params: DossierRequestInput) -> str:
    return await _forward("mosaic_request_dossier", params)


@_tool("mosaic_get_result")
async def mosaic_get_result(params: ResultInput) -> str:
    return await _forward("mosaic_get_result", params)


def main() -> None:
    """The `mosaic-mcp` command: the thin client over stdio."""
    ap = argparse.ArgumentParser(
        prog="mosaic-mcp",
        description="Mosaic's five MCP tools over stdio, forwarded to the hosted service.")
    ap.add_argument("--transport", choices=["stdio"], default="stdio",
                    help="stdio only; for remote access connect to the hosted endpoint")
    ap.add_argument("--version", action="version", version=f"mosaic-mcp {__version__}")
    ap.parse_args()
    # stderr: stdout is the MCP stdio transport.
    if os.environ.get("DATABASE_URL"):
        print("mosaic-mcp: DATABASE_URL is ignored. 3.0 is a thin client to the hosted "
              "service; the bring-your-own-database mode is deprecated (pin mosaic-mcp<3 "
              "to keep it).", file=sys.stderr)
    if not (os.environ.get("MOSAIC_API_KEY") or "").strip():
        print("mosaic-mcp: MOSAIC_API_KEY is not set; every tool call will say so. "
              "Or connect your client to https://mcp.getmosaic.dev/mcp (OAuth).",
              file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
