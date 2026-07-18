"""
Mosaic Pre-Clinical Intelligence MCP Server.

Exposes the pharma knowledge graph through 44 MCP tools for use with
Claude Desktop, Claude Code, or any MCP-compatible client.
"""

# NOTE: do NOT add `from __future__ import annotations` here. The mcp SDK
# (>=1.x) inspects raw `param.annotation` in Tool.from_function; PEP 563
# stringized annotations make it call issubclass(<str>, Context) and crash.

import datetime
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
        f"Upgrade at https://mosaic.bio/pricing"
    )


def _enforce_limit(tool_name: str, requested: int) -> int:
    """Enforce tier-based result limits."""
    tier = _get_session_tier()
    limits = TIER_RESULT_LIMITS.get(tool_name)
    if not limits:
        return requested
    max_allowed = limits.get(tier, limits.get(Tier.FREE, requested))
    return min(requested, max_allowed)

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


_PROV_CACHE: dict[str, Any] = {"at": 0.0, "as_of": None}


def _provenance_as_of() -> str:
    """KG freshness date for the _provenance stamp (cached 5 min)."""
    now = time.time()
    if _PROV_CACHE["as_of"] and now - _PROV_CACHE["at"] < 300:
        return _PROV_CACHE["as_of"]
    as_of = None
    try:
        meta = _gq().get_kg_metadata()
        if meta and meta.get("last_refresh_at"):
            as_of = str(meta["last_refresh_at"])[:10]
    except Exception:
        pass
    as_of = as_of or datetime.date.today().isoformat()
    _PROV_CACHE.update(at=now, as_of=as_of)
    return as_of


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
        data = {
            **data,
            "_provenance": {
                "sources": ["mosaic_kg"],
                "as_of": _provenance_as_of(),
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


class WatchlistCreateInput(BaseModel):
    """Create a watchlist for a user/anon owner key."""
    model_config = ConfigDict(str_strip_whitespace=True)
    owner_key: str = Field(..., description="Owner identifier: user id, email, or 'anon:<token>'", min_length=1, max_length=200)
    name: str = Field(default="My watchlist", description="Watchlist name", max_length=120)


class WatchlistAddItemInput(BaseModel):
    """Add a watched entity to a watchlist."""
    model_config = ConfigDict(str_strip_whitespace=True)
    watchlist_id: str = Field(..., description="Watchlist UUID", min_length=1, max_length=64)
    item_type: str = Field(..., description="One of: target, indication, organization, compound, relation_type", min_length=1, max_length=40)
    item_value: str = Field(..., description="The entity value, e.g. 'EGFR' or 'non_small_cell_lung_carcinoma'", min_length=1, max_length=200)


class WatchlistIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    watchlist_id: str = Field(..., description="Watchlist UUID", min_length=1, max_length=64)


class WatchlistOwnerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    owner_key: str = Field(..., description="Owner identifier: user id, email, or 'anon:<token>'", min_length=1, max_length=200)


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
# Tool 1: search_targets
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_search_targets",
    annotations={
        "title": "Search Drug Targets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_search_targets(params: SearchTargetsInput) -> str:
    """Search the knowledge graph for drug targets by gene symbol or keyword.

    Returns matching targets with basic metadata. Optionally filter by
    therapeutic indication. Use this as the starting point to explore targets.

    Returns:
        JSON list of matching targets with gene_symbol, name, target_class,
        and counts of related compounds, patents, and papers.
    """
    gq = _gq()
    results = gq.search_targets(params.query, params.indication)
    return _json_result(format_search_results(results, params.query))


# ---------------------------------------------------------------------------
# Tool 2: get_target_profile
# ---------------------------------------------------------------------------

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
                f"(curated oncology target set, as of {_provenance_as_of()}). "
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
    return _json_result({
        "target": symbol,
        "compounds": compounds,
        "total": len(compounds),
    })


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
    return _json_result({
        "target": symbol,
        "patents": patents,
        "total": len(patents),
    })


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
    return _json_result({
        "target": symbol,
        "papers": papers,
        "total": len(papers),
    })


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
    name="mosaic_assess_druggability",
    annotations={
        "title": "Assess Target Druggability (AlphaFold + fpocket)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_assess_druggability(params: GeneSymbolInput) -> str:
    """Assess structural druggability of a target from AlphaFold + fpocket.

    Returns the top binding pockets (volume, druggability score), pocket
    count, and a coarse `structural_tier` (highly_druggable / druggable /
    challenging / undruggable) along with a plain-English interpretation.
    This is the structural answer to "is this target small-molecule
    tractable?" — orthogonal to literature-derived druggability heuristics.
    """
    _check_tool_access("mosaic_assess_druggability")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    structure = gq.get_target_structure(symbol)
    return _json_result(format_druggability(structure, symbol))


# ---------------------------------------------------------------------------
# Tool 6: competitive_landscape
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
    name="mosaic_compound_selectivity",
    annotations={
        "title": "Compound Selectivity Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_compound_selectivity(params: CompoundIdInput) -> str:
    """Get the selectivity profile of a compound across all targets.

    Shows activity values against every target the compound has been tested on.
    Critical for assessing off-target effects and safety liability.
    """
    _check_tool_access("mosaic_compound_selectivity")
    gq = _gq()
    result = gq.get_compound_selectivity_profile(params.compound_id.strip())
    return _json_result(format_compound_selectivity(result))


# ---------------------------------------------------------------------------
# Tool 9: indication_landscape
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_indication_landscape",
    annotations={
        "title": "Indication Landscape",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_indication_landscape(params: IndicationInput) -> str:
    """Get the full therapeutic landscape for a disease indication.

    Shows all targets implicated in this indication, compounds in development,
    and clinical status.
    """
    _check_tool_access("mosaic_indication_landscape")
    gq = _gq()
    result = gq.get_indication_landscape(params.indication_name.strip().lower())
    return _json_result(format_indication_landscape(result))


# ---------------------------------------------------------------------------
# Tool 10: list_indications
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_list_indications",
    annotations={
        "title": "List Indications",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_list_indications() -> str:
    """List all therapeutic indications available in the knowledge graph.

    Returns every indication with the number of associated targets.
    Use this to discover which disease areas are loaded.
    """
    gq = _gq()
    indications = gq.list_indications()
    return _json_result({
        "indications": indications,
        "total": len(indications),
    })


# ---------------------------------------------------------------------------
# Tool 10b/10c: sub-indication taxonomy (Move 1, Task 1.1.3)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_list_subindications",
    annotations={
        "title": "List Sub-Indications",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_list_subindications(params: ListSubindicationsInput) -> str:
    """List fine-grained oncology sub-indications in the knowledge graph.

    Sub-indications are histology- or biomarker-defined cancer subtypes
    (e.g. 'EGFR-mutant NSCLC', 'triple-negative breast cancer') organized
    under broader parent indications. Optionally pass `parent_indication`
    (id, name, or synonym, e.g. 'lung cancer' or 'NSCLC') to list only its
    children. Each entry includes the number of linked targets.
    """
    gq = _gq()
    rows = gq.list_subindications(params.parent_indication)
    return _json_result({
        "subindications": rows,
        "total": len(rows),
        "parent_filter": params.parent_indication,
    })


@mcp.tool(
    name="mosaic_subindication_breakdown",
    annotations={
        "title": "Target Sub-Indication Breakdown",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_subindication_breakdown(params: GeneSymbolInput) -> str:
    """Break a target's oncology associations down by sub-indication.

    For the given gene, returns the most relevant cancer sub-indications
    (e.g. EGFR -> NSCLC subtypes vs. colorectal) with the evidence type
    and confidence of each link. Complements mosaic_get_target_profile
    with finer indication granularity.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    rows = gq.get_subindication_breakdown(symbol)
    return _json_result({
        "gene_symbol": symbol,
        "subindication_breakdown": rows,
        "total": len(rows),
    })


@mcp.tool(
    name="mosaic_target_wishlist_add",
    annotations={
        "title": "Request Out-of-Scope Target",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_wishlist_add(params: TargetWishlistInput) -> str:
    """Request a target Mosaic does not yet cover.

    Use this when a gene is outside the current covered set so the
    operator can prioritise it in the next ingestion batch. Idempotent:
    re-requesting the same gene/email bumps a counter. Also returns the
    closest covered targets so the user still gets a useful answer.
    """
    gq = _gq()
    sym = params.gene_symbol.strip().upper()
    if gq.target_exists(sym):
        return _json_result({
            "gene_symbol": sym,
            "already_covered": True,
            "message": f"{sym} is already covered — use mosaic_get_target_profile.",
        })
    rec = gq.add_target_wishlist(sym, params.user_email, params.notes)
    return _json_result({
        "gene_symbol": sym,
        "added_to_wishlist": True,
        "request_count": rec.get("request_count"),
        "closest_covered_targets": gq.find_closest_targets(sym),
        "message": (
            f"{sym} is out of current scope. Logged your request "
            f"(seen {rec.get('request_count', 1)}x). Closest covered "
            f"targets are listed so you're not blocked."
        ),
    })


@mcp.tool(
    name="mosaic_watchlist_create",
    annotations={"title": "Create Watchlist", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
@_with_db_error_handling
def mosaic_watchlist_create(params: WatchlistCreateInput) -> str:
    """Create a watchlist to track targets, indications, orgs, or compounds.

    owner_key is the user id, email, or an 'anon:<token>' for anonymous
    sessions. Returns the new watchlist id to use with
    mosaic_watchlist_add_item.
    """
    wl = _gq().create_watchlist(params.owner_key, params.name)
    return _json_result({"watchlist": wl, "created": bool(wl)})


@mcp.tool(
    name="mosaic_watchlist_add_item",
    annotations={"title": "Add Watchlist Item", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
@_with_db_error_handling
def mosaic_watchlist_add_item(params: WatchlistAddItemInput) -> str:
    """Add a watched entity (target/indication/organization/compound/
    relation_type) to a watchlist. Idempotent — re-adding is a no-op."""
    ok = _gq().add_watchlist_item(
        params.watchlist_id, params.item_type, params.item_value
    )
    if not ok:
        return _json_result({"error": "watchlist not found",
                             "watchlist_id": params.watchlist_id})
    return _json_result({
        "watchlist_id": params.watchlist_id,
        "added": {"item_type": params.item_type.lower(),
                  "item_value": params.item_value},
    })


@mcp.tool(
    name="mosaic_watchlist_get",
    annotations={"title": "Get Watchlist", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
@_with_db_error_handling
def mosaic_watchlist_get(params: WatchlistIdInput) -> str:
    """Get a watchlist with its items and recent detected events."""
    wl = _gq().get_watchlist(params.watchlist_id)
    if wl is None:
        return _json_result({"error": "watchlist not found",
                             "watchlist_id": params.watchlist_id})
    return _json_result(wl)


@mcp.tool(
    name="mosaic_watchlist_list",
    annotations={"title": "List Watchlists", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
@_with_db_error_handling
def mosaic_watchlist_list(params: WatchlistOwnerInput) -> str:
    """List an owner's watchlists with item and recent-event counts."""
    rows = _gq().list_watchlists(params.owner_key)
    return _json_result({"watchlists": rows, "total": len(rows)})


# ---------------------------------------------------------------------------
# Tool 11: target_scores
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_target_scores",
    annotations={
        "title": "Target Attractiveness Scores",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_scores(params: GeneSymbolInput) -> str:
    """Get computed attractiveness scores for a drug target.

    Returns overall target attractiveness, scientific validation, druggability,
    competitive intensity, and research momentum (0-1 scale) with direction.
    """
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_target_scores(symbol)
    if result is None:
        return _json_result({"error": f"No scores computed for '{symbol}'"})
    return _json_result(result)


# ---------------------------------------------------------------------------
# Tool 12: target_validation
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_target_validation",
    annotations={
        "title": "Target Validation Evidence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_validation(params: GeneSymbolInput) -> str:
    """Get experimental validation evidence for a drug target.

    Returns genetic (CRISPR/siRNA), in vivo (animal models), clinical
    (patient data), and pharmacological validation evidence from literature.
    Includes specific papers with model systems and outcomes.
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

@mcp.tool(
    name="mosaic_compound_analogs",
    annotations={
        "title": "Compound Structural Analogs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_compound_analogs(params: CompoundIdWithLimit) -> str:
    """Get structural analogs of a compound with Tanimoto similarity.

    Returns analogs with similarity scores, shared scaffolds, and their
    activity against targets. Useful for SAR analysis and lead optimization.
    """
    _check_tool_access("mosaic_compound_analogs")
    gq = _gq()
    cid = params.compound_id.strip()
    limit = _enforce_limit("mosaic_compound_analogs", params.limit)
    analogs = gq.get_compound_series(cid)
    return _json_result({"compound_id": cid, "analogs": analogs, "total": len(analogs)})


# ---------------------------------------------------------------------------
# Tool 15: compare_targets
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_compare_targets",
    annotations={
        "title": "Compare Targets Side-by-Side",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_compare_targets(params: MultiGeneInput) -> str:
    """Side-by-side comparison of 2-5 drug targets.

    Returns compound counts, patent counts, paper counts, best IC50,
    max clinical phase, attractiveness scores, and momentum for each target.
    """
    _check_tool_access("mosaic_compare_targets")
    gq = _gq()
    results = gq.compare_targets(params.gene_symbols)
    return _json_result(format_compare_targets(results))


# ---------------------------------------------------------------------------
# Tool 16: find_whitespace_opportunities
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_find_opportunities",
    annotations={
        "title": "Find White-Space Opportunities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_find_opportunities(params: OpportunityInput) -> str:
    """Find underexplored high-potential drug targets — white-space opportunities.

    Identifies targets with high scientific validation but low competitive
    intensity. These are the best opportunities for novel drug programs
    where the biology is strong but Big Pharma hasn't crowded the space.

    Ranked by opportunity_score = validation × (1 - competition) × momentum_boost.
    """
    _check_tool_access("mosaic_find_opportunities")
    gq = _gq()
    results = gq.find_whitespace_opportunities(params.therapy_area, params.limit)

    return _json_result({
        "_meta": {
            "tool": "mosaic_find_opportunities",
            "description": (
                f"White-space drug target opportunities"
                + (f" in {params.therapy_area}" if params.therapy_area else "")
            ),
            "scoring": "opportunity_score = scientific_validation × (1 - competitive_intensity) × momentum_boost",
            "hint": "Use mosaic_get_target_profile on any gene_symbol for the full dossier.",
        },
        "opportunities": results,
        "total": len(results),
        "filter": params.therapy_area,
    })


# ---------------------------------------------------------------------------
# Tool 16b: find_undruggable_targets
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_find_undruggable_targets",
    annotations={
        "title": "Find Structurally Intractable Targets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_find_undruggable_targets(params: UndruggableInput) -> str:
    """Find validated targets that are structurally hard to hit with small molecules.

    Returns targets in the 'challenging' or 'undruggable' tier (or with a
    top fpocket druggability score below the threshold), plus their pipeline
    gap signals (compound count, approved drug count, validation count) and
    a suggested modality (PROTAC / glue, biologic / PPI, fragment-based,
    or allosteric SBDD). This is the white-space tool for new-modality
    programs.

    Ranked by opportunity_score = validation × (1 - top_pocket_score) ×
    (1 - competitive_intensity).
    """
    _check_tool_access("mosaic_find_undruggable_targets")
    gq = _gq()
    rows = gq.find_undruggable_targets(
        therapy_area=params.therapy_area,
        max_pocket_score=params.max_pocket_score,
        min_disorder_frac=params.min_disorder_frac,
        require_validation=params.require_validation,
        limit=params.limit,
    )
    return _json_result(format_undruggable_targets(rows, params.therapy_area))


# ---------------------------------------------------------------------------
# Tool: synthetic_lethal_whitespace (KG-native, Move 2 Task 2.4.R.1)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_synthetic_lethal_whitespace",
    annotations={
        "title": "Synthetic-Lethal Whitespace",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_synthetic_lethal_whitespace(params: SyntheticLethalInput) -> str:
    """Find synthetic-lethal *whitespace*: targets functionally coupled to
    a developed (drugged) target but themselves undeveloped.

    For an anchor target with chemical matter, surfaces partners coupled to
    it by DepMap co-essentiality, STRING protein-protein interactions and/or
    Reactome pathways. Returns TWO lists, deliberately not merged:

    - `candidates` — partners Mosaic covers, filtered to < 5 patents and no
      clinical compound. Those counts are measured, so a whitespace claim is
      supportable. Ranked by `whitespace_score`.
    - `coupled_unassessed` — partners outside the curated universe. They are
      coupled to the anchor, but their competitive status is UNKNOWN rather
      than zero, so they carry null counts and no whitespace_score, and are
      ranked by `coupling_strength` alone. Leads, not evidence. Do not
      describe them as uncontested.

    Scope: DepMap co-essentiality IS ingested; `co_functionality_basis` says
    per candidate whether a row used it (`depmap_coessentiality`) or fell
    back to the PPI + shared-pathway proxy (`ppi_pathway_proxy`). `lineage`
    is still a label only — it does not filter. Hypothesis generator.
    """
    _check_tool_access("mosaic_synthetic_lethal_whitespace")
    gq = _gq()
    result = gq.find_synthetic_lethal_whitespace(
        approved_target=params.approved_target,
        lineage=params.lineage,
        limit=params.limit,
    )

    if not result.get("candidates"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_synthetic_lethal_whitespace",
                **empty_scope_note(
                    params.approved_target or "the developed-target set",
                    "synthetic-lethal whitespace candidates",
                    hint="Needs STRING PPI / Reactome pathway coupling to a "
                         "drugged anchor; ensure those layers are ingested.",
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })

    return _json_result({
        "_meta": {
            "tool": "mosaic_synthetic_lethal_whitespace",
            "description": (
                "Synthetic-lethal whitespace partners for "
                + (params.approved_target or "top developed targets")
            ),
            "scoring": (
                "whitespace_score = co_functionality_proxy x 1/(1+patents_B) "
                "x (1 + ln(1+validation_evidence_B))"
            ),
            "caveat": result.get("method"),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool: modality_gaps (KG-native, Move 2 Task 2.4.R.3)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_modality_gaps",
    annotations={
        "title": "Modality Gaps",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_modality_gaps(params: ModalityGapsInput) -> str:
    """Which compound modalities are explored vs absent for a target.

    Modality is a heuristic SMILES classification (small_molecule,
    covalent, degrader, macrocycle, peptide_like) over the top-ranked
    compounds — partial coverage by design; `unclassified` is reported
    explicitly. Target-level only: protein_family is not populated, so
    family-level rollups are unavailable (stated, not silently wrong).
    """
    _check_tool_access("mosaic_modality_gaps")
    gq = _gq()
    result = gq.find_modality_gaps(params.target_or_family)
    if not result.get("explored"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_modality_gaps",
                **empty_scope_note(
                    params.target_or_family, "modality coverage",
                    hint="No classified compounds for this target. Run "
                         "scripts/classify_compound_modality.py to widen "
                         "coverage.",
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })
    return _json_result({
        "_meta": {
            "tool": "mosaic_modality_gaps",
            "description": (
                f"Modality coverage for {result.get('resolved_target')}"
            ),
            "caveat": result.get("note"),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool: resistance_bypass_map (KG-native, Move 2 Task 2.4.R.2)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_resistance_bypass_map",
    annotations={
        "title": "Resistance Bypass Map",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_resistance_bypass_map(params: ResistanceBypassInput) -> str:
    """Candidate resistance-bypass / escape targets for a given target.

    From a deterministic keyword pass over the literature
    (resistance_relations — GLiREL has no resistance edge type), surfaces
    targets co-mentioned with the query target in resistance-context
    abstracts, ranked by a drugability-gap score (strong resistance
    evidence, low development activity). Hypothesis generator, not
    evidence — every row carries its source snippet.
    """
    _check_tool_access("mosaic_resistance_bypass_map")
    gq = _gq()
    result = gq.find_resistance_bypass_map(
        target=params.target, indication=params.indication
    )
    if not result.get("bypass_candidates"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_resistance_bypass_map",
                **empty_scope_note(
                    params.target, "resistance-bypass candidates",
                    hint="No resistance-context co-mentions found. Run "
                         "scripts/extract_resistance_relations.py if the "
                         "resistance_relations layer is unpopulated.",
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })
    return _json_result({
        "_meta": {
            "tool": "mosaic_resistance_bypass_map",
            "description": (
                f"Resistance-bypass candidates for {result['target']}"
            ),
            "scoring": (
                "drugability_gap = evidence_papers x avg_confidence "
                "x 1/(1+patents_bypass)"
            ),
            "caveat": result.get("method"),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool: talent_migration (KG-native, Move 2 Task 2.4.R.4)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_talent_migration",
    annotations={
        "title": "Researcher / Talent Migration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_talent_migration(params: TalentMigrationInput) -> str:
    """Who works on a target, recency-weighted, and what *else* they
    work on — a talent-flow signal.

    Surfaces the most active researchers on a target (publication-based,
    using the resolved persons table) and, for each, the other targets
    they've published on over time — i.e. "people who worked on X now
    also on Y". Patent inventors are not included (not ingested).
    """
    _check_tool_access("mosaic_talent_migration")
    gq = _gq()
    result = gq.find_talent_migration(
        target=params.target, lookback_years=params.lookback_years
    )
    if not result.get("researchers"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_talent_migration",
                **empty_scope_note(
                    params.target, "researcher activity",
                    hint="No authored papers mention this target in the "
                         "lookback window.",
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })
    return _json_result({
        "_meta": {
            "tool": "mosaic_talent_migration",
            "description": (
                f"Researchers active on {result['target']} "
                f"(last {params.lookback_years}y), with their other targets"
            ),
            "ranking": "recency_weight = sum of (paper_year - window_start + 1)",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool: emerging_signals (KG-native, Move 2 Task 2.4.R.5)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_emerging_signals",
    annotations={
        "title": "Emerging Activity Signals",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_emerging_signals(params: EmergingSignalsInput) -> str:
    """Targets whose recent literature/patent activity significantly
    exceeds their own prior baseline (z-score > 2).

    Simple statistics over monthly counts — no ML. Use to spot targets
    heating up before they crowd. `sparkline` is recent monthly counts.
    """
    _check_tool_access("mosaic_emerging_signals")
    gq = _gq()
    result = gq.find_emerging_signals(
        window_months=params.window_months,
        signal_type=params.signal_type,
        limit=params.limit,
    )
    if not result.get("signals"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_emerging_signals",
                **empty_scope_note(
                    f"any target ({params.signal_type})",
                    "emerging activity surge",
                    hint="No target cleared z>2 vs its own baseline in "
                         "this window.",
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })
    return _json_result({
        "_meta": {
            "tool": "mosaic_emerging_signals",
            "description": (
                f"{params.signal_type} surges (z>2) over the last "
                f"{params.window_months} months"
            ),
            "scoring": "z = (recent_window - baseline_mean) / baseline_stdev",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool: find_similar_targets (KG-native, Move 3 Task M3.1)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_find_similar_targets",
    annotations={
        "title": "Find Similar Targets (structural)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_find_similar_targets(params: SimilarTargetsInput) -> str:
    """Structurally similar targets to a given gene, ranked by Foldseek
    TM-score over the AlphaFold PDB corpus.

    Returns the top-k neighbours (default 10) with neighbour metadata
    (name, target_class, druggability_tier) and the structural-similarity
    metrics (tm_score normalised over query length, alntmscore over
    alignment length, evalue, lddt, rmsd). Use for paralog / fold-analog
    discovery, scaffold-hopping target ideation, and cross-family
    chemistry repurposing.

    Source table is populated by `scripts/build_foldseek_index.py`. When
    the index hasn't been built yet, returns an empty result with a
    populate-hint rather than failing — degrades cleanly on fresh DBs.
    """
    _check_tool_access("mosaic_find_similar_targets")
    gq = _gq()
    result = gq.find_similar_targets(target=params.gene, k=params.k)

    if not result.get("neighbors"):
        hint = (
            "Build the Foldseek structural-similarity index: "
            "`python -m scripts.build_foldseek_index`."
            if not result.get("index_loaded")
            else "No structural neighbours found for this gene."
        )
        return _json_result({
            "_meta": {
                "tool": "mosaic_find_similar_targets",
                **empty_scope_note(
                    params.gene, "structural neighbours",
                    hint=hint,
                    as_of=_provenance_as_of(),
                ),
            },
            **result,
        })
    return _json_result({
        "_meta": {
            "tool": "mosaic_find_similar_targets",
            "description": (
                f"Top-{params.k} structurally similar targets to "
                f"{result['target']}"
            ),
            "scoring": (
                "Foldseek 3Di+AA all-vs-all; ranking by TM-score "
                "normalised over query length"
            ),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 17: org_portfolio
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_org_portfolio",
    annotations={
        "title": "Organization Portfolio",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_org_portfolio(params: OrgNameInput) -> str:
    """Get a pharma/biotech organization's full portfolio.

    Shows which drug targets they're active on, their patent filings,
    therapy area focus, and competitive positioning. Use to understand
    what a company is working on and where they're investing.
    """
    _check_tool_access("mosaic_org_portfolio")
    gq = _gq()
    result = gq.get_org_portfolio(params.org_name)
    return _json_result(result)


# ---------------------------------------------------------------------------
# Tool 18: target_network_map
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_target_network",
    annotations={
        "title": "Target Knowledge Graph Network",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_network(params: GeneSymbolInput) -> str:
    """Get the full knowledge graph network around a drug target.

    Returns all connected entities (compounds, diseases, pathways,
    organizations, interacting proteins) as nodes and edges. Shows how
    a target connects to the broader drug discovery landscape.

    Useful for understanding the full context of a target and finding
    non-obvious connections.
    """
    _check_tool_access("mosaic_target_network")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_target_network(symbol)
    return _json_result(result)


# ---------------------------------------------------------------------------
# Tool 19: target_mechanisms (semantic relations)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_target_mechanisms",
    annotations={
        "title": "Target Mechanism of Action Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_target_mechanisms(params: GeneSymbolInput) -> str:
    """Get the mechanism-of-action profile for a drug target.

    Returns how compounds interact with this target — inhibitors (covalent,
    allosteric, competitive), agonists, antagonists, degraders (PROTAC).
    Also shows semantic edge types: validation evidence, resistance mechanisms,
    biomarker roles, safety concerns, and clinical efficacy signals.

    Extracted by GLiREL from paper and patent abstracts.
    """
    _check_tool_access("mosaic_target_mechanisms")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_target_moa_profile(symbol)

    if not result or result.get("total_semantic_relations", 0) == 0:
        return _json_result({
            "_meta": {
                "tool": "mosaic_target_mechanisms",
                **empty_scope_note(
                    symbol, "mechanism-of-action / semantic relations",
                    hint="Semantic extraction may not have been run yet "
                         "(scripts/run_semantic_extraction.py).",
                    as_of=_provenance_as_of(),
                ),
            },
            "target": symbol,
        })

    moa = result.get("moa_summary", {})
    edges = result.get("semantic_edge_counts", {})

    return _json_result({
        "_meta": {
            "tool": "mosaic_target_mechanisms",
            "description": f"Mechanism-of-action profile for {symbol}",
            "interpretation": (
                f"{result['total_semantic_relations']} semantic relations. "
                f"MOA types: {', '.join(k + ' (' + str(v.get('count', 0)) + ')' for k, v in moa.items())}. "
                + (f"Paper/patent edges: {', '.join(f'{k} ({v})' for k, v in edges.items())}."
                   if edges else "")
            ),
        },
        "target": symbol,
        "moa_summary": moa,
        "semantic_edge_counts": edges,
        "moa_compounds": result.get("moa_compounds", [])[:15],
        "total_relations": result.get("total_semantic_relations", 0),
    })


# ---------------------------------------------------------------------------
# Tool 20: evidence_map (semantic relations)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_evidence_map",
    annotations={
        "title": "Target Evidence Map",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_evidence_map(params: GeneSymbolInput) -> str:
    """Get the full evidence landscape for a drug target from semantic extraction.

    Shows all relation types (validation, resistance, biomarker, safety, efficacy,
    expression, pathway, drug target ID) broken down by source type (paper vs patent),
    with confidence stats and top evidence snippets per relation type.

    Use this to understand the strength and breadth of evidence for a target.
    """
    _check_tool_access("mosaic_evidence_map")
    gq = _gq()
    symbol = params.gene_symbol.strip().upper()
    result = gq.get_evidence_map(symbol)

    if not result.get("evidence_by_type"):
        return _json_result({
            "_meta": {
                "tool": "mosaic_evidence_map",
                **empty_scope_note(
                    symbol, "semantic evidence",
                    as_of=_provenance_as_of(),
                ),
            },
            "target": symbol,
        })

    # Apply confidence filtering to evidence items
    for etype, edata in result.get("evidence_by_type", {}).items():
        if isinstance(edata, dict) and "top_evidence" in edata:
            edata["top_evidence"] = _filter_by_confidence(
                edata["top_evidence"], key="confidence"
            )

    return _json_result({
        "_meta": {
            "tool": "mosaic_evidence_map",
            "description": f"Evidence landscape for {symbol}",
            "confidence_thresholds": {
                "display_minimum": CONFIDENCE_DISPLAY_THRESHOLD,
                "high_confidence": CONFIDENCE_HIGH_THRESHOLD,
            },
            "summary": (
                f"{result['source_documents']} source docs, "
                f"{result['compound_partners']} compound partners, "
                f"{result['target_partners']} target partners"
            ),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 21: relation_search
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_relation_search",
    annotations={
        "title": "Search by Semantic Relation Type",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_relation_search(params: RelationSearchInput) -> str:
    """Search the entire knowledge graph for entity pairs with a specific relation type.

    Returns the highest-confidence entity pairs for a given relation
    (e.g. all 'degrades_protac' relations, or all 'resistance_mechanism' edges).
    Useful for cross-target analysis like "which targets have PROTAC degraders?"
    or "where are resistance mechanisms documented?"
    """
    _check_tool_access("mosaic_relation_search")
    gq = _gq()
    result = gq.get_relation_search(
        params.relation_type, params.min_confidence, params.limit
    )

    # Add confidence badges to results
    if "results" in result:
        result["results"] = _filter_by_confidence(result["results"], key="confidence")

    return _json_result({
        "_meta": {
            "tool": "mosaic_relation_search",
            "description": f"KG-wide search for '{params.relation_type}' relations",
            "confidence_thresholds": {
                "display_minimum": CONFIDENCE_DISPLAY_THRESHOLD,
                "high_confidence": CONFIDENCE_HIGH_THRESHOLD,
            },
            "total_in_kg": result.get("total_in_kg", 0),
            "showing": len(result.get("results", [])),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 22: compound_polypharmacology
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_compound_polypharmacology",
    annotations={
        "title": "Compound Polypharmacology Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_compound_polypharmacology(params: CompoundNameInput) -> str:
    """Get the polypharmacology profile of a compound — all targets it interacts with.

    Shows every target the compound has semantic relations with, the mechanism
    of action for each (inhibits, agonizes, degrades, etc.), and evidence counts.
    Useful for understanding off-target effects, repurposing potential, and
    selectivity from a semantic (not just activity) perspective.
    """
    _check_tool_access("mosaic_compound_polypharmacology")
    gq = _gq()
    result = gq.get_polypharmacology(params.compound_name)

    if not result.get("found"):
        return _json_result({
            "_meta": {"tool": "mosaic_compound_polypharmacology"},
            "error": f"Compound '{params.compound_name}' not found. Try the exact drug name (e.g. 'imatinib').",
        })

    return _json_result({
        "_meta": {
            "tool": "mosaic_compound_polypharmacology",
            "description": f"Polypharmacology profile for {result['compound']}",
            "summary": f"{result['target_count']} targets with semantic MOA data",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 23: kg_stats
# ---------------------------------------------------------------------------

@mcp.tool(
    name="mosaic_kg_stats",
    annotations={
        "title": "Knowledge Graph Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_kg_stats() -> str:
    """Get overall statistics for the Mosaic knowledge graph.

    Returns entity counts (targets, compounds, papers, patents), semantic
    relation totals and breakdown by type, coverage metrics, and ChEMBL
    activity counts. Use this to understand the scope and coverage of the KG.
    """
    gq = _gq()
    cached = gq.get_kg_metadata()
    if cached:
        result = {
            "targets": cached.get("target_count"),
            "compounds": cached.get("compound_count"),
            "papers": cached.get("paper_count"),
            "patents": cached.get("patent_count"),
            "semantic_relations": cached.get("relation_count"),
            "subindications": cached.get("subindication_count"),
            "therapy_areas": cached.get("therapy_areas"),
            "data_sources": cached.get("data_sources"),
            "last_refresh_at": cached.get("last_refresh_at"),
            "source": "kg_metadata",
        }
    else:
        # Fall back to live COUNT(*) if the cache hasn't been refreshed.
        result = {**gq.get_kg_stats(), "source": "live_count"}
    return _json_result({
        "_meta": {
            "tool": "mosaic_kg_stats",
            "description": "Mosaic KG overview",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 24: trial_results  (Task 21 — real CT.gov data)
# ---------------------------------------------------------------------------

class TrialResultsInput(BaseModel):
    """Filters for the clinical trial results search."""
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbol: str | None = Field(default=None, description="Gene symbol (e.g. 'EGFR'). At least one filter required.")
    compound_name: str | None = Field(default=None, description="Compound / drug name (e.g. 'osimertinib')")
    indication_name: str | None = Field(default=None, description="Disease name (e.g. 'lung cancer')")
    has_results_only: bool = Field(default=False, description="Only return trials with posted results")
    limit: int = Field(default=25, ge=1, le=100)


@mcp.tool(
    name="mosaic_trial_results",
    annotations={
        "title": "Clinical Trial Results (ClinicalTrials.gov)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_trial_results(params: TrialResultsInput) -> str:
    """Return real ClinicalTrials.gov records (NCT ID, title, phase, sponsor, status).

    Filters by any combination of gene symbol, compound name, or indication.
    Unlike `mosaic_clinical_pipeline` which can synthesize from max_phase, every
    row returned here has a real NCT ID and brief title. Use this when you need
    to cite specific trials or highlight read-outs.
    """
    _check_tool_access("mosaic_trial_results")

    if not (params.gene_symbol or params.compound_name or params.indication_name):
        return _json_error("Provide at least one of: gene_symbol, compound_name, indication_name")

    gq = _gq()
    result = gq.get_trial_results(
        target_symbol=params.gene_symbol,
        compound_name=params.compound_name,
        indication_name=params.indication_name,
        has_results_only=params.has_results_only,
        limit=params.limit,
    )
    return _json_result({
        "_meta": {
            "tool": "mosaic_trial_results",
            "description": "Real ClinicalTrials.gov records from the Mosaic trials table",
            "data_source": "clinicaltrials.gov v2 API (ingested via fetch_clinical_trials.py)",
            "total": result.get("total_shown", 0),
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 25: regulatory_status  (Task 22 — openFDA)
# ---------------------------------------------------------------------------

class DrugNameInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    drug_name: str = Field(..., description="Brand or generic drug name (e.g. 'imatinib')", min_length=2, max_length=100)


@mcp.tool(
    name="mosaic_regulatory_status",
    annotations={
        "title": "FDA Regulatory Status (openFDA)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,  # Hits external openFDA API
    },
)
def mosaic_regulatory_status(params: DrugNameInput) -> str:
    """Query openFDA for a drug's approval status, label indications, and adverse events.

    Returns: FDA approval dates, brand/generic names, sponsor, product type,
    route of administration, and a summary count of serious adverse events.
    Data is live from openFDA — does not require local ingestion.
    """
    _check_tool_access("mosaic_regulatory_status")

    import httpx

    name = params.drug_name.strip()
    out: dict[str, Any] = {
        "_meta": {
            "tool": "mosaic_regulatory_status",
            "data_source": "openFDA (https://api.fda.gov)",
            "query": name,
        },
        "drug_name": name,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            # Drug label (indications, route, sponsor)
            label_resp = client.get(
                "https://api.fda.gov/drug/label.json",
                params={"search": f'openfda.brand_name:"{name}" OR openfda.generic_name:"{name}"', "limit": 1},
            )
            if label_resp.status_code == 200:
                data = label_resp.json()
                results = data.get("results") or []
                if results:
                    r = results[0]
                    openfda = r.get("openfda", {}) or {}
                    out["label"] = {
                        "brand_names": openfda.get("brand_name") or [],
                        "generic_names": openfda.get("generic_name") or [],
                        "manufacturer": (openfda.get("manufacturer_name") or [None])[0],
                        "route": openfda.get("route") or [],
                        "product_type": openfda.get("product_type") or [],
                        "application_number": (openfda.get("application_number") or [None])[0],
                        "indications_and_usage": (r.get("indications_and_usage") or [None])[0],
                        "boxed_warning": (r.get("boxed_warning") or [None])[0],
                    }
                else:
                    out["label"] = None
            elif label_resp.status_code == 404:
                out["label"] = None
            else:
                out["label_error"] = f"openFDA label API {label_resp.status_code}"

            # Adverse event count
            ae_resp = client.get(
                "https://api.fda.gov/drug/event.json",
                params={"search": f'patient.drug.medicinalproduct:"{name}"', "limit": 1},
            )
            if ae_resp.status_code == 200:
                meta = ae_resp.json().get("meta", {}) or {}
                out["adverse_events_reported"] = meta.get("results", {}).get("total", 0)
            else:
                out["adverse_events_reported"] = None

    except Exception as e:
        logger.warning("openFDA error: %s", e)
        return _json_error(f"openFDA lookup failed: {e}")

    return _json_result(out)


# ---------------------------------------------------------------------------
# Tool 26: compare_drugs  (Task 23)
# ---------------------------------------------------------------------------

class CompareDrugsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    drug_a: str = Field(..., min_length=2, max_length=100)
    drug_b: str = Field(..., min_length=2, max_length=100)


@mcp.tool(
    name="mosaic_compare_drugs",
    annotations={
        "title": "Compare Two Drugs Head-to-Head",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_compare_drugs(params: CompareDrugsInput) -> str:
    """Side-by-side comparison of two compounds.

    Returns max_phase, first approval year, molecule type, shared targets,
    unique targets per side, and per-target potency (pChEMBL) for both. Use
    this for competitive analyses like "osimertinib vs erlotinib" or
    "imatinib vs dasatinib".
    """
    _check_tool_access("mosaic_compare_drugs")
    gq = _gq()
    result = gq.compare_drugs(params.drug_a, params.drug_b)
    return _json_result({
        "_meta": {
            "tool": "mosaic_compare_drugs",
            "description": f"Head-to-head: {params.drug_a} vs {params.drug_b}",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 27: drug_repurposing  (Task 24)
# ---------------------------------------------------------------------------

class RepurposingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    compound_name: str = Field(..., min_length=2, max_length=100)
    limit: int = Field(default=15, ge=1, le=50)


@mcp.tool(
    name="mosaic_drug_repurposing",
    annotations={
        "title": "Drug Repurposing Candidates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_drug_repurposing(params: RepurposingInput) -> str:
    """Find new indications where a compound's primary targets are implicated.

    Returns indications where the compound's targets have supporting evidence
    but the compound itself is not yet clinically active. Ranked by a simple
    target-support × avg-evidence score. Useful as a starting point for
    repurposing hypotheses — not a substitute for clinical review.
    """
    _check_tool_access("mosaic_drug_repurposing")
    gq = _gq()
    result = gq.find_repurposing_candidates(params.compound_name, limit=params.limit)
    return _json_result({
        "_meta": {
            "tool": "mosaic_drug_repurposing",
            "description": f"Repurposing candidates for {params.compound_name}",
            "ranking": "target_support × avg_evidence (proxy; not clinically validated)",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Tool 28: kol_finder  (Task 25)
# ---------------------------------------------------------------------------

class KolFinderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    gene_symbol: str | None = Field(default=None, description="Gene symbol to find KOLs for")
    indication_name: str | None = Field(default=None, description="Disease to find KOLs for")
    limit: int = Field(default=15, ge=1, le=50)


@mcp.tool(
    name="mosaic_kol_finder",
    annotations={
        "title": "Key Opinion Leader (KOL) Finder",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_db_error_handling
def mosaic_kol_finder(params: KolFinderInput) -> str:
    """Rank the top authors (KOLs) publishing on a target or indication.

    Scores by paper volume with recency weighting (papers since 2023 count 2x).
    Returns person name, paper count, most recent publication date, and
    affiliated organizations. Useful for advisory board assembly, trial PI
    scouting, and competitive intelligence.
    """
    _check_tool_access("mosaic_kol_finder")
    if not (params.gene_symbol or params.indication_name):
        return _json_error("Provide either gene_symbol or indication_name")
    gq = _gq()
    result = gq.find_kols(
        target_symbol=params.gene_symbol,
        indication_name=params.indication_name,
        limit=params.limit,
    )
    return _json_result({
        "_meta": {
            "tool": "mosaic_kol_finder",
            "description": "Top authors ranked by recency-weighted paper count",
            "scoring": "papers in last 3y count 2x; ordered by weighted_score",
        },
        **result,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server (stdio transport by default)."""
    import argparse

    parser = argparse.ArgumentParser(description="Mosaic Pre-Clinical Intelligence MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=3001, help="Port for SSE transport")
    args = parser.parse_args()

    if args.transport == "sse":
        # Remote transport requires Bearer auth (ADR mcp-auth.md).
        import uvicorn

        from mosaic_mcp.auth import build_authenticated_sse_app

        uvicorn.run(
            build_authenticated_sse_app(mcp),
            host="0.0.0.0",
            port=args.port,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
