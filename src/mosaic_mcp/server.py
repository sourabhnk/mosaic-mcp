"""
Mosaic Pre-Clinical Intelligence MCP Server.

Exposes the pharma knowledge graph through 44 MCP tools for use with
Claude Desktop, Claude Code, or any MCP-compatible client.
"""

# NOTE: do NOT add `from __future__ import annotations` here. The mcp SDK
# (>=1.x) inspects raw `param.annotation` in Tool.from_function; PEP 563
# stringized annotations make it call issubclass(<str>, Context) and crash.

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Ensure src imports resolve when running as standalone script
# ---------------------------------------------------------------------------
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from mosaic_mcp.db.connection import ConnectionManager, get_read_pool
from mosaic_mcp.db.queries import GraphQueries
from mosaic_mcp.users import (
    FREE_TOOLS,
    TIER_RESULT_LIMITS,
    Tier,
)
from mosaic_mcp.responses import (
    format_target_dossier,
    format_competitive_landscape,
    format_pathway_context,
    format_compound_selectivity,
    format_search_results,
    format_indication_landscape,
    format_compare_targets,
    format_clinical_pipeline,
    format_validation_summary,
    format_structure_summary,
    format_druggability,
    format_undruggable_targets,
    empty_scope_note,
    WELL_KNOWN_TARGETS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence thresholds for MCP output
# ---------------------------------------------------------------------------

CONFIDENCE_DISPLAY_THRESHOLD = 0.1     # Minimum confidence to show a relation
CONFIDENCE_HIGH_THRESHOLD = 0.3        # "high confidence" badge threshold

def _confidence_badge(confidence: float | None) -> str:
    """Return a confidence badge string based on thresholds."""
    if confidence is None:
        return "unknown"
    if confidence >= CONFIDENCE_HIGH_THRESHOLD:
        return "high"
    if confidence >= CONFIDENCE_DISPLAY_THRESHOLD:
        return "moderate"
    return "low"

def _filter_by_confidence(items: list[dict], key: str = "confidence") -> list[dict]:
    """Filter items below the display threshold and add confidence badges."""
    filtered = []
    for item in items:
        conf = item.get(key)
        if conf is not None and conf < CONFIDENCE_DISPLAY_THRESHOLD:
            continue
        item["confidence_badge"] = _confidence_badge(conf)
        filtered.append(item)
    return filtered


# ---------------------------------------------------------------------------
# Tool tier gating
# ---------------------------------------------------------------------------

# Current user tier for MCP session (set via env var or API key validation)
_session_tier: Tier = Tier.FREE


def _get_session_tier() -> Tier:
    """Resolve the tier for the current request.

    Remote (SSE/HTTP) requests carry an authenticated principal whose
    tier comes from the resolved API key (ADR docs/decisions/mcp-auth.md).
    The MOSAIC_TIER / MOSAIC_API_KEY env path is retained only for local
    stdio dev where there is no per-request auth.
    """
    from mosaic_mcp.auth import principal_tier

    resolved = principal_tier()
    if resolved is not None:
        return resolved

    tier_env = os.getenv("MOSAIC_TIER", "").lower()
    if tier_env in ("pro", "enterprise", "admin"):
        return Tier(tier_env)
    api_key = os.getenv("MOSAIC_API_KEY", "")
    if api_key:
        # stdio dev only: a valid env key implies Pro access.
        return Tier.PRO
    return Tier.FREE


class PaidTierRequired(Exception):
    """Raised when a free-tier session calls a Pro-only tool.

    Raising (rather than returning a JSON string) makes FastMCP mark the
    tool response with `isError: true`, so Claude Desktop / any MCP client
    knows the call failed and can surface the upgrade prompt cleanly
    instead of treating the error payload as a valid tool result.
    """


def _check_tool_access(tool_name: str) -> None:
    """Enforce tier gating for a tool. Raises PaidTierRequired on denial."""
    tier = _get_session_tier()
    if tier in (Tier.PRO, Tier.ENTERPRISE, Tier.ADMIN):
        return
    if tool_name in FREE_TOOLS:
        return
    raise PaidTierRequired(
        f"Tool '{tool_name}' requires a Pro plan ($49/mo). "
        f"Free tier includes: {', '.join(sorted(FREE_TOOLS))}. "
        f"Upgrade at https://getmosaic.dev/pricing"
    )


def _enforce_limit(tool_name: str, requested: int) -> int:
    """Enforce tier-based result limits."""
    tier = _get_session_tier()
    limits = TIER_RESULT_LIMITS.get(tool_name)
    if not limits:
        return requested
    max_allowed = limits.get(tier, limits.get(Tier.FREE, requested))
    return min(requested, max_allowed)


def _paged(rows: list[dict], key: str, **extra) -> dict:
    """Build a paged payload that tells the truth about what it withheld.

    Hand-ported from the hosted server (board S7b). `sync_pip_package.py`
    syncs the data layer but deliberately EXCLUDES server.py, and server.py is
    what *calls* it -- so a query-layer change lands here only if a human
    carries it. queries.py now emits `total_available` (COUNT(*) OVER (),
    evaluated before LIMIT); without this function the pip package would leak
    that column onto every row AND keep reporting the page size as `total`.
    """
    has_total = bool(rows) and "total_available" in rows[0]
    total = int(rows[0]["total_available"]) if has_total else len(rows)
    clean = [{k: v for k, v in r.items() if k != "total_available"} for r in rows]
    payload = {**extra, key: clean, "returned": len(clean), "total": total}
    if rows and not has_total:
        # The query layer drifted out from under this copy. Say so rather than
        # crashing OR silently republishing the page size as the total.
        payload["_total_is_page_size"] = True
    if total > len(clean):
        payload["truncated"] = True
        payload["_note"] = (
            f"Showing {len(clean)} of {total}. Raise `limit`, or upgrade if you "
            f"are at your tier's cap — see https://getmosaic.dev/pricing"
        )
    return payload

# ---------------------------------------------------------------------------
# Lifespan — shared DB connection
# ---------------------------------------------------------------------------

_graph_queries: GraphQueries | None = None


def _get_graph_queries() -> GraphQueries:
    """Lazily initialise a shared GraphQueries instance.

    Uses the read pool which auto-routes to Neon pooler / read replica.
    """
    global _graph_queries
    if _graph_queries is None:
        _graph_queries = GraphQueries(get_read_pool())
    return _graph_queries


@asynccontextmanager
async def _lifespan(server: Any):
    """Initialise graph connection on startup, close on shutdown."""
    gq = _get_graph_queries()
    yield {"graph_queries": gq}
    gq.db.close()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "mosaic_mcp",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Label enrichment safety-net
# ---------------------------------------------------------------------------
#
# Historical bug: tools returned `compound_id: "CHEMBL25"` with no sibling
# `compound_name`, so LLM clients surfaced raw ChEMBL IDs to users. The real
# fix lives in the query layer (COALESCE(pref_name, name, chembl_id)), but
# this walker catches any regression by ensuring every `*_id` field in the
# response tree has a readable sibling label. It batches lookups and caches
# per-process to avoid hammering Postgres on every tool call.

import re as _re

_ID_KEYS_TO_NAME_KEY: dict[str, str] = {
    "compound_id": "compound_name",
    "target_id": "target_name",
    "indication_id": "indication_name",
    "paper_id": "paper_title",
    "patent_id": "patent_title",
    "nct_id": "trial_title",
}

_LABEL_CACHE: dict[tuple[str, str], str] = {}  # (kind, id) -> display label
_LABEL_CACHE_MAX = 5000


def _lookup_labels(kind: str, ids: set[str]) -> dict[str, str]:
    """Batch-resolve IDs to display labels; cache results per-process."""
    if not ids:
        return {}
    out: dict[str, str] = {}
    missing: list[str] = []
    for i in ids:
        key = (kind, i)
        if key in _LABEL_CACHE:
            out[i] = _LABEL_CACHE[key]
        else:
            missing.append(i)
    if not missing:
        return out

    try:
        gq = _gq()
        if kind == "compound":
            rows = gq.db.execute(
                "SELECT id, COALESCE(pref_name, name, chembl_id, id) AS label "
                "FROM compounds WHERE id = ANY(%s)",
                (missing,),
            )
        elif kind == "target":
            rows = gq.db.execute(
                "SELECT id, COALESCE(symbol, name, id) AS label "
                "FROM targets WHERE id = ANY(%s)",
                (missing,),
            )
        elif kind == "indication":
            rows = gq.db.execute(
                "SELECT id, COALESCE(name, id) AS label "
                "FROM indications WHERE id = ANY(%s)",
                (missing,),
            )
        elif kind == "paper":
            rows = gq.db.execute(
                "SELECT paper_id AS id, COALESCE(title, paper_id) AS label "
                "FROM papers WHERE paper_id = ANY(%s)",
                (missing,),
            )
        elif kind == "patent":
            rows = gq.db.execute(
                "SELECT patent_id AS id, COALESCE(title, patent_id) AS label "
                "FROM patents WHERE patent_id = ANY(%s)",
                (missing,),
            )
        elif kind == "trial":
            rows = gq.db.execute(
                "SELECT nct_id AS id, COALESCE(brief_title, nct_id) AS label "
                "FROM trials WHERE nct_id = ANY(%s)",
                (missing,),
            )
        else:
            rows = []
    except Exception as e:
        logger.debug("label lookup failed for %s: %s", kind, e)
        rows = []

    for r in rows or []:
        rid = r.get("id")
        label = r.get("label")
        if rid and label:
            out[rid] = label
            if len(_LABEL_CACHE) < _LABEL_CACHE_MAX:
                _LABEL_CACHE[(kind, rid)] = label
    # Any still missing fall back to the raw ID string
    for i in missing:
        out.setdefault(i, i)
    return out


def _collect_ids(node: Any, buckets: dict[str, set[str]]) -> None:
    """Walk the response tree and collect IDs needing enrichment."""
    if isinstance(node, dict):
        for id_key, name_key in _ID_KEYS_TO_NAME_KEY.items():
            if id_key in node and node.get(id_key) and not node.get(name_key):
                kind = id_key.replace("_id", "")
                if kind == "nct":
                    kind = "trial"
                val = node[id_key]
                if isinstance(val, str):
                    buckets.setdefault(kind, set()).add(val)
        for v in node.values():
            _collect_ids(v, buckets)
    elif isinstance(node, list):
        for item in node:
            _collect_ids(item, buckets)


def _apply_labels(node: Any, resolved: dict[str, dict[str, str]]) -> None:
    """Inject sibling *_name fields where missing."""
    if isinstance(node, dict):
        for id_key, name_key in _ID_KEYS_TO_NAME_KEY.items():
            if id_key in node and node.get(id_key) and not node.get(name_key):
                kind = id_key.replace("_id", "")
                if kind == "nct":
                    kind = "trial"
                val = node[id_key]
                if isinstance(val, str):
                    label = resolved.get(kind, {}).get(val)
                    if label:
                        node[name_key] = label
        for v in node.values():
            _apply_labels(v, resolved)
    elif isinstance(node, list):
        for item in node:
            _apply_labels(item, resolved)


def _enrich_labels(data: Any) -> Any:
    """Safety net: ensure every `*_id` in a response has a sibling name field.

    Mutates the tree in place and returns it. Any lookup failures are silent —
    this is a best-effort overlay on top of the query layer's COALESCE logic.
    """
    try:
        buckets: dict[str, set[str]] = {}
        _collect_ids(data, buckets)
        if not buckets:
            return data
        resolved: dict[str, dict[str, str]] = {
            kind: _lookup_labels(kind, ids) for kind, ids in buckets.items()
        }
        _apply_labels(data, resolved)
    except Exception as e:
        logger.debug("label enrichment skipped: %s", e)
    return data


# Freshness states for the _provenance stamp. Emitted on BOTH paths, so the
# field's PRESENCE never carries meaning — only its value does.
FRESHNESS_KG_METADATA = "kg_metadata"  # read a real last_refresh_at; as_of is set
FRESHNESS_UNAVAILABLE = "unavailable"  # could not read it; as_of is null, NOT today

# `at` = when we last ATTEMPTED, not when we last succeeded. Gating on the
# attempt (rather than on a truthy value) is load-bearing: the old guard was
# `if _PROV_CACHE["as_of"] and ...`, so once as_of stops being truthy every tool
# re-queries on every call — a retry storm against a database that is, by
# construction, already unhealthy. Unknown is cached too, briefly.
_PROV_CACHE: dict[str, Any] = {"at": 0.0, "as_of": None, "freshness": None}
_PROV_TTL_OK_S = 300.0
_PROV_TTL_UNAVAILABLE_S = 45.0  # short: a recovered DB must not keep reporting unknown


def _provenance_freshness() -> tuple[str | None, str]:
    """KG freshness for the _provenance stamp: ``(as_of, freshness)``.

    Returns ``as_of=None`` when ``kg_metadata`` cannot be read. It used to fall
    back to ``datetime.date.today()``, which stamped EVERY tool response with
    today's date whenever the lookup failed — rendering "we do not know how fresh
    this is" as "this is maximally fresh". A plausible date is worse than no date
    because nothing downstream can tell it was invented.
    """
    now = time.time()
    cached_state = _PROV_CACHE["freshness"]
    if cached_state is not None:
        ttl = _PROV_TTL_OK_S if cached_state == FRESHNESS_KG_METADATA else _PROV_TTL_UNAVAILABLE_S
        if now - _PROV_CACHE["at"] < ttl:
            return _PROV_CACHE["as_of"], cached_state

    as_of: str | None = None
    try:
        meta = _gq().get_kg_metadata()
        if meta and meta.get("last_refresh_at"):
            as_of = str(meta["last_refresh_at"])[:10]
    except Exception as e:
        logger.warning("provenance: kg_metadata unreadable, as_of=null: %s", e)

    freshness = FRESHNESS_KG_METADATA if as_of else FRESHNESS_UNAVAILABLE
    _PROV_CACHE.update(at=now, as_of=as_of, freshness=freshness)
    return as_of, freshness


def _provenance_as_of() -> str | None:
    """The freshness DATE alone, or None when it cannot be read.

    Kept as a separate accessor because ~10 call sites interpolate this into
    prose. They are falsy-guarded, so None correctly drops the "(as of ...)"
    clause instead of asserting a date nobody measured.
    """
    return _provenance_freshness()[0]


def _as_of_clause() -> str:
    """", as of YYYY-MM-DD" — or nothing when freshness is unreadable.

    The one site that interpolates the date BARE, so it needs its own guard or
    it prints "as of None".
    """
    as_of = _provenance_as_of()
    return f", as of {as_of}" if as_of else ""


def _json_result(data: Any) -> str:
    """Serialise a result dict/list to compact JSON.

    Runs the label-enrichment safety net first so any `*_id` field that slipped
    through without a sibling name gets one before the client sees it. Then
    stamps a standard `_provenance` block (Task 1.2.3) on dict results that
    don't already carry one — additive, never overwrites a tool-specific
    provenance (e.g. external-source tools).
    """
    data = _enrich_labels(data)
    if (
        isinstance(data, dict)
        and "_provenance" not in data
        and "error" not in data
    ):
        _as_of, _freshness = _provenance_freshness()
        data = {
            **data,
            "_provenance": {
                "sources": ["mosaic_kg"],
                # Always present, null when unknown — an absent key would
                # mean "this build does not stamp freshness", a different fact.
                "as_of": _as_of,
                "freshness": _freshness,
                "confidence_summary": None,
            },
        }
    return json.dumps(data, indent=2, default=str)


def _json_error(message: str) -> str:
    """Return a JSON error envelope."""
    return json.dumps({"error": message}, indent=2)


def _gq() -> GraphQueries:
    return _get_graph_queries()


def _tier_allows(tool_name: str) -> bool:
    """Same predicate as _check_tool_access, without raising."""
    if _get_session_tier() in (Tier.PRO, Tier.ENTERPRISE, Tier.ADMIN):
        return True
    return tool_name in FREE_TOOLS


class WatchlistOwnerRequired(Exception):
    """Raised when a session supplies no usable owner key."""


def _watchlist_owner_key(requested: str | None) -> str:
    """Resolve the owner identity for a watchlist operation.

    `owner_key` used to be free text taken straight off the request and
    documented as "user id, email, or 'anon:<token>'", so passing a customer's
    email address listed their watchlists. That the field was untrustworthy is
    not hypothetical: of the 9 rows in `watchlists` at the time of the fix, six
    had a **gene symbol** as the owner key (EGFR, PIK3CA, ABL1, KDR, MAPK1) —
    LLM clients filling an ambiguous "owner" field with whatever was in
    context. A field models populate by guessing cannot be an identity.

    This is the stdio package, which has no request principal (`auth.py` is a
    stub — see sync_from_monorepo/_compat notes), so unlike the hosted server
    there is no authenticated account to derive an owner from. Every session is
    therefore confined to the `anon:` namespace, which cannot collide with the
    hosted `user:<uuid>` keys. The token is a bearer secret the caller chose;
    scope it accordingly.
    """
    req = (requested or "").strip()
    if req.startswith("anon:") and len(req) > len("anon:"):
        return req
    raise WatchlistOwnerRequired(
        "Watchlists need an identity. Pass owner_key='anon:<a random token you "
        "keep>'. A bare id or email is not accepted — it would let one caller "
        "name another. (The pip package is stdio-only and has no signed-in "
        "account; use the hosted MCP server if you want watchlists tied to "
        "your Mosaic account.)"
    )


def _with_db_error_handling(fn):
    """Catch DB connection errors and add Op-6 response caching.

    Cache reads are free (no quota call here — the MCP tool path does
    not meter). The cache key includes the session tier so a free user
    can never receive a Pro-only or higher-limit cached result.
    """
    import functools

    import psycopg

    from mosaic_mcp import cache as _cache

    tool_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        params = args[0] if args else None
        cache_params = None
        if (
            params is not None
            and hasattr(params, "model_dump")
            and tool_name not in _cache.NON_CACHEABLE
            and _tier_allows(tool_name)
        ):
            try:
                cache_params = {
                    **params.model_dump(),
                    "__tier": _get_session_tier().value,
                }
                hit = _cache.tool_cache_get(tool_name, cache_params)
                if hit is not None:
                    return hit
            except Exception as e:  # never let caching break a tool
                logger.debug("tool cache get skipped: %s", e)
                cache_params = None

        try:
            result = fn(*args, **kwargs)
        except psycopg.OperationalError as e:
            msg = str(e).split("\n")[0]
            return _json_error(
                f"Database connection failed: {msg}. "
                "Is PostgreSQL running? Check DATABASE_URL environment variable."
            )

        if (
            cache_params is not None
            and isinstance(result, str)
            and '"error"' not in result
        ):
            try:
                _cache.tool_cache_put(tool_name, cache_params, result)
            except Exception as e:
                logger.debug("tool cache put skipped: %s", e)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# Input Models
# ---------------------------------------------------------------------------

class SearchTargetsInput(BaseModel):
    """Input for searching targets."""
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(..., description="Gene symbol or keyword to search (e.g. 'EGFR', 'kinase')", min_length=1, max_length=200)
    indication: str | None = Field(default=None, description="Optional indication filter (e.g. 'oncology')")


class GeneSymbolInput(BaseModel):
    """Input requiring a single gene symbol."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbol: str = Field(..., description="Gene symbol of the target (e.g. 'EGFR', 'BRAF', 'TP53')", min_length=1, max_length=30)


class GeneSymbolWithLimit(BaseModel):
    """Input requiring a gene symbol with optional limit."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbol: str = Field(..., description="Gene symbol of the target (e.g. 'EGFR', 'BRAF')", min_length=1, max_length=30)
    limit: int = Field(default=20, description="Maximum number of results to return", ge=1, le=200)


class CompoundIdInput(BaseModel):
    """Input requiring a compound identifier."""
    model_config = ConfigDict(str_strip_whitespace=True)
    compound_id: str = Field(..., description="Compound identifier — ChEMBL ID (e.g. 'CHEMBL25') or internal ID", min_length=1, max_length=100)


class IndicationInput(BaseModel):
    """Input requiring an indication name."""
    model_config = ConfigDict(str_strip_whitespace=True)
    indication_name: str = Field(..., description="Indication/disease name (e.g. 'oncology', 'breast_cancer')", min_length=1, max_length=200)


class ListSubindicationsInput(BaseModel):
    """Optional parent filter for listing sub-indications."""
    model_config = ConfigDict(str_strip_whitespace=True)
    parent_indication: str | None = Field(default=None, description="Optional parent indication id, name, or synonym (e.g. 'lung cancer', 'NSCLC') to list only its children", max_length=200)


class TargetWishlistInput(BaseModel):
    """Request that an out-of-scope target be added in a future ingest."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbol: str = Field(..., description="Gene symbol to request (e.g. 'GPR55')", min_length=1, max_length=50)
    user_email: str = Field(default="anonymous", description="Optional requester email so they can be notified", max_length=200)
    notes: str | None = Field(default=None, description="Optional context for why this target matters", max_length=1000)


# `owner_key` was a free-text identity ("user id, email, or 'anon:<token>'"),
# which meant naming someone returned their watchlists. It is now restricted to
# the `anon:` namespace and validated by `_watchlist_owner_key`. `watchlist_id`
# alone is no longer sufficient for read or write — see `get_watchlist` and
# `add_watchlist_item` in db/queries.py, which scope on owner in the SQL.
_OWNER_KEY_FIELD = Field(
    default=None,
    description=(
        "'anon:<token>' you generate and keep. This is the stdio package: it "
        "has no signed-in account, so watchlists are anonymous and the token "
        "is what proves the list is yours."
    ),
    max_length=200,
)


class WatchlistCreateInput(BaseModel):
    """Create a watchlist owned by the caller."""
    model_config = ConfigDict(str_strip_whitespace=True)
    owner_key: str | None = _OWNER_KEY_FIELD
    name: str = Field(default="My watchlist", description="Watchlist name", max_length=120)


class WatchlistAddItemInput(BaseModel):
    """Add a watched entity to one of the caller's watchlists."""
    model_config = ConfigDict(str_strip_whitespace=True)
    watchlist_id: str = Field(..., description="Watchlist UUID", min_length=1, max_length=64)
    item_type: str = Field(..., description="One of: target, indication, organization, compound, relation_type", min_length=1, max_length=40)
    item_value: str = Field(..., description="The entity value, e.g. 'EGFR' or 'non_small_cell_lung_carcinoma'", min_length=1, max_length=200)
    owner_key: str | None = _OWNER_KEY_FIELD


class WatchlistIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    watchlist_id: str = Field(..., description="Watchlist UUID", min_length=1, max_length=64)
    owner_key: str | None = _OWNER_KEY_FIELD


class WatchlistOwnerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    owner_key: str | None = _OWNER_KEY_FIELD


class CompoundIdWithLimit(BaseModel):
    """Input requiring a compound ID with optional limit."""
    model_config = ConfigDict(str_strip_whitespace=True)
    compound_id: str = Field(..., description="Compound identifier (e.g. 'CHEMBL25')", min_length=1, max_length=100)
    limit: int = Field(default=10, description="Maximum analogs to return", ge=1, le=50)


class MultiGeneInput(BaseModel):
    """Input for comparing multiple gene symbols."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbols: list[str] = Field(..., description="2-5 gene symbols to compare (e.g. ['EGFR', 'BRAF', 'ALK'])", min_length=2, max_length=5)


class OpportunityInput(BaseModel):
    """Input for finding whitespace opportunities."""
    model_config = ConfigDict(str_strip_whitespace=True)
    therapy_area: str | None = Field(default=None, description="Optional therapy area filter (e.g. 'oncology', 'cardiovascular')")
    limit: int = Field(default=20, description="Max results", ge=1, le=50)


class SyntheticLethalInput(BaseModel):
    """Input for synthetic-lethal whitespace discovery."""
    model_config = ConfigDict(str_strip_whitespace=True)
    approved_target: str | None = Field(
        default=None,
        description="Anchor target with chemical matter (e.g. 'KRAS'). "
        "If omitted, the most-developed targets are used as anchors.",
    )
    lineage: str | None = Field(
        default=None,
        description="Tumor lineage filter (reserved — active once DepMap "
        "co-essentiality is ingested; currently a no-op label).",
    )
    limit: int = Field(default=20, description="Max pairs", ge=1, le=50)


class ModalityGapsInput(BaseModel):
    """Input for modality-gap analysis."""
    model_config = ConfigDict(str_strip_whitespace=True)
    target_or_family: str = Field(
        ..., min_length=1, max_length=100,
        description="Target gene symbol (e.g. 'BRD4'). Family-level is "
        "not available — protein_family data is unpopulated.",
    )


class ResistanceBypassInput(BaseModel):
    """Input for resistance-bypass mapping."""
    model_config = ConfigDict(str_strip_whitespace=True)
    target: str = Field(..., description="Target whose therapies face "
                        "resistance (e.g. 'EGFR')", min_length=1, max_length=100)
    indication: str | None = Field(
        default=None,
        description="Optional indication context (reserved — paper-"
        "indication links not modelled; currently a label only).",
    )


class TalentMigrationInput(BaseModel):
    """Input for talent-migration discovery."""
    model_config = ConfigDict(str_strip_whitespace=True)
    target: str = Field(..., description="Target gene symbol (e.g. 'EGFR')",
                         min_length=1, max_length=100)
    lookback_years: int = Field(default=5, ge=1, le=20,
                                description="Years of history to weight")


class EmergingSignalsInput(BaseModel):
    """Input for emerging-signal detection."""
    model_config = ConfigDict(str_strip_whitespace=True)
    window_months: int = Field(default=6, ge=1, le=24,
                               description="Recent window length in months")
    signal_type: str = Field(
        default="paper_surge",
        pattern=r"^(paper_surge|patent_surge)$",
        description="paper_surge or patent_surge",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Max signals")


class SimilarTargetsInput(BaseModel):
    """Input for structural-similarity search (Move 3 Task M3.1)."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene: str = Field(
        ..., description="Target gene symbol or UniProt id (e.g. 'EGFR')",
        min_length=1, max_length=100,
    )
    k: int = Field(
        default=10, ge=1, le=50,
        description="Max number of structurally similar neighbours to return",
    )


class UndruggableInput(BaseModel):
    """Input for finding structurally intractable targets."""
    model_config = ConfigDict(str_strip_whitespace=True)
    therapy_area: str | None = Field(
        default=None,
        description="Optional therapy area filter (e.g. 'oncology', 'neuroscience')",
    )
    max_pocket_score: float = Field(
        default=0.4,
        description="Targets with top fpocket druggability score below this are eligible",
        ge=0.0, le=1.0,
    )
    min_disorder_frac: float = Field(
        default=0.0,
        description=(
            "If > 0, also include targets with this fraction of pLDDT<50 "
            "(disordered) residues, regardless of pocket score"
        ),
        ge=0.0, le=1.0,
    )
    require_validation: bool = Field(
        default=True,
        description="Require validation evidence (paper_validations or scientific_validation >= 0.3)",
    )
    limit: int = Field(default=25, description="Max targets to return", ge=1, le=100)


class OrgNameInput(BaseModel):
    """Input requiring an organization name."""
    model_config = ConfigDict(str_strip_whitespace=True)
    org_name: str = Field(..., description="Organization name or partial name (e.g. 'Pfizer', 'Novartis', 'Roche')", min_length=2, max_length=200)


class CompoundNameInput(BaseModel):
    """Input requiring a compound name."""
    model_config = ConfigDict(str_strip_whitespace=True)
    compound_name: str = Field(..., description="Compound name (e.g. 'imatinib', 'osimertinib', 'pembrolizumab')", min_length=2, max_length=200)


class RelationSearchInput(BaseModel):
    """Input for searching by semantic relation type."""
    model_config = ConfigDict(str_strip_whitespace=True)
    relation_type: str = Field(
        ...,
        description=(
            "Semantic relation type. Options: "
            "validates_therapeutic_target, resistance_mechanism, biomarker, "
            "safety_concern, clinical_efficacy, expression_change, "
            "pathway_involvement, drug_target_identification, "
            "inhibits, inhibits_covalent, inhibits_allosteric, inhibits_competitive, "
            "agonizes, antagonizes, degrades_protac, modulates_allosteric, partial_agonist"
        ),
    )
    min_confidence: float = Field(default=0.1, description="Minimum confidence threshold (0.0-1.0)", ge=0.0, le=1.0)
    limit: int = Field(default=30, description="Maximum results to return", ge=1, le=100)


# ---------------------------------------------------------------------------
# Tool 0: start_here — the front door
# ---------------------------------------------------------------------------
#
# Ported from the hosted server, ADAPTED — not copied. Three things are true on
# the hosted side and false here, and each would be a confident wrong answer:
#
#   1. Hosted enforces a monthly query quota and a daily unique-target limit.
#      This package enforces NEITHER — it gates tools (`_check_tool_access`) and
#      counts nothing. Reporting "50 queries/month" here would state a limit that
#      does not exist. Omitted rather than guessed.
#   2. Hosted's free tier is activated by a magic link. There is no magic link in
#      a stdio package; tier comes from MOSAIC_TIER / MOSAIC_API_KEY.
#   3. Hosted serves Mosaic's curated KG. **This package is bring-your-own-
#      database** — it queries whatever DATABASE_URL points at, and there is no
#      shared Mosaic database to connect to. That is the single most important
#      orientation fact for a pip user and the hosted text never says it, because
#      for a hosted user it is not true.

_CAPABILITY_GROUPS: list[dict] = [
    {"group": "Start with one target",
     "answers": "Everything known about a single gene — profile, scores, druggability, assay precedent.",
     "tools": ["mosaic_search_targets", "mosaic_get_target_profile", "mosaic_target_scores",
               "mosaic_assess_druggability", "mosaic_target_validation", "mosaic_get_target_structure"],
     "example": "mosaic_get_target_profile(gene_symbol='EGFR')"},
    {"group": "Compounds & chemistry",
     "answers": "What binds a target, how selective it is, its analogs and polypharmacology.",
     "tools": ["mosaic_get_target_compounds", "mosaic_compound_selectivity", "mosaic_compound_analogs",
               "mosaic_compound_polypharmacology", "mosaic_modality_gaps", "mosaic_compare_drugs"],
     "example": "mosaic_compound_selectivity(compound_id='CHEMBL941')"},
    {"group": "Clinical & regulatory",
     "answers": "Trials for a target's drugs, real ClinicalTrials.gov records, FDA status, repurposing.",
     "tools": ["mosaic_clinical_pipeline", "mosaic_trial_results", "mosaic_regulatory_status",
               "mosaic_drug_repurposing"],
     "example": "mosaic_clinical_pipeline(gene_symbol='ERBB2')"},
    {"group": "Competitive & IP",
     "answers": "Who is working on a target — patents, organizations, KOLs, talent flow.",
     "tools": ["mosaic_competitive_landscape", "mosaic_get_target_patents", "mosaic_org_portfolio",
               "mosaic_kol_finder", "mosaic_talent_migration"],
     "example": "mosaic_competitive_landscape(gene_symbol='KRAS')"},
    {"group": "Discovery & white-space",
     "answers": "Find targets: underexplored, undruggable, synthetic-lethal, resistance-bypass, emerging, similar.",
     "tools": ["mosaic_find_opportunities", "mosaic_find_undruggable_targets",
               "mosaic_synthetic_lethal_whitespace", "mosaic_resistance_bypass_map",
               "mosaic_emerging_signals", "mosaic_find_similar_targets", "mosaic_compare_targets"],
     "example": "mosaic_find_opportunities(therapy_area='oncology')"},
    {"group": "Biology & evidence",
     "answers": "Pathways, network neighborhood, mechanism of action, papers, and the raw evidence trail.",
     "tools": ["mosaic_pathway_context", "mosaic_target_network", "mosaic_target_mechanisms",
               "mosaic_evidence_map", "mosaic_relation_search", "mosaic_get_target_papers"],
     "example": "mosaic_pathway_context(gene_symbol='BRAF')"},
    {"group": "Indications",
     "answers": "Targets and compounds for a disease, and fine-grained oncology sub-indications.",
     "tools": ["mosaic_indication_landscape", "mosaic_list_indications",
               "mosaic_list_subindications", "mosaic_subindication_breakdown"],
     "example": "mosaic_indication_landscape(indication_name='non-small cell lung cancer')"},
    {"group": "Track & request",
     "answers": "Save targets/orgs to a watchlist, request a target we don't cover yet, or check KG scope.",
     "tools": ["mosaic_watchlist_create", "mosaic_watchlist_add_item", "mosaic_watchlist_get",
               "mosaic_watchlist_list", "mosaic_target_wishlist_add", "mosaic_kg_stats"],
     "example": "mosaic_kg_stats()"},
]


@mcp.tool(
    name="mosaic_get_target_profile",
    annotations={
        "title": "Get Target Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_get_target_profile(params: GeneSymbolInput) -> str:
    """Get a comprehensive intelligence dossier for a drug target.

    Returns UniProt biology, target scores, SAR summary, disease associations,
    validation evidence, pathways, PPIs, competitive landscape, clinical
    pipeline, and publication momentum. This is the primary tool for any
    target question.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    profile = gq.get_target_deep_profile(symbol)

    if profile is None:
        wellknown = symbol in WELL_KNOWN_TARGETS
        return _json_result({
            "error": (
                f"'{symbol}' is not in the current Mosaic KG "
                f"(curated oncology target set{_as_of_clause()}). "
                "This is a coverage statement, not a claim that the gene "
                "does not exist."
            ),
            "target": symbol,
            "wishlist_cta": (
                f"{symbol} is a well-characterised target — flag it via "
                "mosaic_target_wishlist_add to prioritise coverage."
                if wellknown else
                "Use mosaic_target_wishlist_add to request coverage for "
                "this target."
            ),
        })

    return _json_result(format_target_dossier(profile))


# ---------------------------------------------------------------------------
# Tool 3: get_target_compounds
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_get_target_compounds",
    annotations={
        "title": "Get Target Compounds",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_get_target_compounds(params: GeneSymbolWithLimit) -> str:
    """Get compounds active against a specific drug target.

    Returns compounds with activity data (IC50, Ki, etc.) sorted by potency.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    limit = _enforce_limit("mosaic_get_target_compounds", params.limit)
    compounds = gq.get_target_compounds(symbol, limit)
    return _json_result(_paged(compounds, "compounds", target=symbol))


# ---------------------------------------------------------------------------
# Tool 4: get_target_patents
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_get_target_patents",
    annotations={
        "title": "Get Target Patents",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_get_target_patents(params: GeneSymbolWithLimit) -> str:
    """Get patents mentioning a specific drug target.

    Returns patent filings with titles, dates, and assignee organizations.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    limit = _enforce_limit("mosaic_get_target_patents", params.limit)
    patents = gq.get_target_patents(symbol, limit)
    return _json_result(_paged(patents, "patents", target=symbol))


# ---------------------------------------------------------------------------
# Tool 5: get_target_papers
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_get_target_papers",
    annotations={
        "title": "Get Target Papers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_get_target_papers(params: GeneSymbolWithLimit) -> str:
    """Get scientific papers mentioning a specific drug target.

    Returns publications from PubMed/OpenAlex with titles and dates.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    limit = _enforce_limit("mosaic_get_target_papers", params.limit)
    papers = gq.get_target_papers(symbol, limit)
    return _json_result(_paged(papers, "papers", target=symbol))


# ---------------------------------------------------------------------------
# Tool 5a: get_target_structure (AlphaFold)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_get_target_structure",
    annotations={
        "title": "Get Target Structure (AlphaFold)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_get_target_structure(params: GeneSymbolInput) -> str:
    """Get the AlphaFold structural snapshot for a drug target.

    Returns AlphaFold model URLs (PDB / CIF / PAE), per-residue confidence
    summary (mean pLDDT, fractions of residues at high / confident / low
    confidence, disordered fraction), and protein length. Useful for
    SBDD scoping, disorder/IDR risk, and confidence-aware target triage.
    Pair with `mosaic_assess_druggability` for binding-pocket scoring.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    structure = gq.get_target_structure(symbol)

    if structure is None:
        return _json_result({
            "_meta": {
                "tool": "mosaic_get_target_structure",
                **empty_scope_note(
                    symbol, "AlphaFold structure",
                    as_of=_provenance_as_of(),
                ),
            },
            "target": symbol,
            "available": False,
        })

    summary = format_structure_summary(structure)
    return _json_result({
        "_meta": {
            "tool": "mosaic_get_target_structure",
            "description": (
                f"AlphaFold structural snapshot for {symbol} "
                f"(UniProt {structure.get('uniprot_id')})"
            ),
        },
        "target": symbol,
        "available": True,
        "summary": summary,
    })


# ---------------------------------------------------------------------------
# Tool 5b: assess_druggability
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_competitive_landscape",
    annotations={
        "title": "Competitive Landscape",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_competitive_landscape(params: GeneSymbolInput) -> str:
    """Get the full competitive landscape for a drug target.

    Multi-hop traversal: Target <- Compounds, Target <- Patents -> Organizations.
    Shows which pharma/biotech companies are active on this target,
    how many patents and compounds each has, and overall competitive intensity.
    """
    _check_tool_access("mosaic_competitive_landscape")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_competitive_landscape(symbol)
    return _json_result(format_competitive_landscape(result))


# ---------------------------------------------------------------------------
# Tool 7: pathway_context
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_pathway_context",
    annotations={
        "title": "Pathway Context",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_pathway_context(params: GeneSymbolInput) -> str:
    """Get pathway context for a drug target.

    Shows which biological pathways the target participates in, other targets
    in the same pathways, and protein-protein interactions.
    """
    _check_tool_access("mosaic_pathway_context")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_pathway_context(symbol)
    return _json_result(format_pathway_context(result))


# ---------------------------------------------------------------------------
# Tool 8: compound_selectivity
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_target_validation",
    annotations={
        "title": "Assay Precedent (Target Validation)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_validation(params: GeneSymbolInput) -> str:
    """Assay precedent for a drug target: what has been tried, in what model system.

    Returns literature-derived experimental precedent — genetic (CRISPR/siRNA/KO),
    in vivo (mouse/rat/xenograft), pharmacological (inhibitor/SAR), and clinical-
    trial evidence — re-classified per paper at query time. A paper may carry
    several methods; counts are lower bounds, not exhaustive. Papers that name the
    target in their title are flagged high-confidence and lead the ranked
    exemplars. Outcomes are NOT auto-graded: open the linked exemplar papers to
    read what actually happened. Also returns DepMap essentiality + AlphaMissense
    pathogenicity when available. A target with no precedent returns an explicit
    no_precedent_in_corpus status (corpus coverage, not confirmed absence).
    """
    _check_tool_access("mosaic_target_validation")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_target_validation_summary(symbol)
    return _json_result(format_validation_summary(result))


# ---------------------------------------------------------------------------
# Tool 13: clinical_pipeline
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_clinical_pipeline",
    annotations={
        "title": "Clinical Trial Pipeline",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_clinical_pipeline(params: GeneSymbolInput) -> str:
    """Get clinical trial pipeline for compounds targeting a gene.

    Returns compounds in clinical development with indications, trial
    phases, and status from ClinicalTrials.gov data.
    """
    _check_tool_access("mosaic_clinical_pipeline")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_clinical_pipeline(symbol)
    return _json_result(format_clinical_pipeline(result))


# ---------------------------------------------------------------------------
# Tool 14: compound_analogs
# ---------------------------------------------------------------------------

