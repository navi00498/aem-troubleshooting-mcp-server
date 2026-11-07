"""Pure functions that turn raw AEM responses into concise diagnoses. No network access here."""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any

from aem_log_analyzer.analyzer import ErrorLogStats, RequestLogStats, build_findings
from aem_log_analyzer.parsers import parse_error_log, parse_request_log

# --- OSGi bundles ----------------------------------------------------------------------------------------------------

BUNDLE_OK_STATES = {"Active", "Fragment"}
BUNDLE_HINTS = {
    "Installed": "Installed means the bundle can't resolve: an imported package is missing or has the wrong version. "
    "Open /system/console/bundles/<id> to see the unresolved Import-Package entries.",
    "Resolved": "Resolved means the bundle resolved but isn't running: it was stopped, or its activator failed. "
    "Check error.log around startup for the bundle's symbolic name.",
}


def _matches(needle: str | None, *values: Any) -> bool:
    if not needle:
        return True
    needle = needle.lower()
    return any(needle in str(value).lower() for value in values if value)


def summarize_bundles(
    data: dict[str, Any], include_all: bool = False, name_contains: str | None = None, limit: int = 100
) -> dict[str, Any]:
    bundles = data.get("data") or []
    by_state = Counter(b.get("state", "Unknown") for b in bundles)
    problems = [b for b in bundles if b.get("state") not in BUNDLE_OK_STATES]
    selected = [
        b
        for b in (bundles if include_all else problems)
        if _matches(name_contains, b.get("symbolicName"), b.get("name"))
    ]
    return {
        "status_line": data.get("status"),
        "total": len(bundles),
        "by_state": dict(by_state.most_common()),
        "problem_count": len(problems),
        "bundles": [
            {
                "id": b.get("id"),
                "symbolic_name": b.get("symbolicName"),
                "name": b.get("name"),
                "version": b.get("version"),
                "state": b.get("state"),
            }
            for b in selected[:limit]
        ],
        "truncated": len(selected) > limit,
        "hints": {state: hint for state, hint in BUNDLE_HINTS.items() if by_state.get(state)},
    }


# --- Declarative Services components ---------------------------------------------------------------------------------

COMPONENT_HINTS = {
    "unsatisfied (reference)": "A mandatory @Reference has no matching service. Open /system/console/components/<id> "
    "to see which reference is unsatisfied, then check that the providing bundle is active and the target filter "
    "matches.",
    "failed activation": "The @Activate method threw an exception. Search error.log for the component name to find "
    "the stack trace.",
    "unsatisfied (configuration)": "The component requires an OSGi configuration that isn't present for this run "
    "mode. Check /apps/<project>/osgiconfig/config.<runmode> for the PID.",
}
COMPONENT_PROBLEM_STATES = tuple(COMPONENT_HINTS)


def summarize_components(data: dict[str, Any], name_contains: str | None = None, limit: int = 100) -> dict[str, Any]:
    components = data.get("data") or []
    by_state = Counter(c.get("state", "unknown") for c in components)
    problems = [
        c
        for c in components
        if c.get("state") in COMPONENT_PROBLEM_STATES and _matches(name_contains, c.get("name"), c.get("pid"))
    ]
    return {
        "total": len(components),
        "by_state": dict(by_state.most_common()),
        "problem_count": sum(by_state.get(state, 0) for state in COMPONENT_PROBLEM_STATES),
        "problems": [
            {"id": c.get("id"), "name": c.get("name"), "state": c.get("state"), "bundle_id": c.get("bundleId")}
            for c in problems[:limit]
        ],
        "truncated": len(problems) > limit,
        "hints": {state: hint for state, hint in COMPONENT_HINTS.items() if by_state.get(state)},
    }


# --- Replication -----------------------------------------------------------------------------------------------------


def list_replication_agents(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Agents from /etc/replication/agents.author.2.json. Credentials (transportUser/Password) are never returned."""
    agents = []
    for agent_id, node in data.items():
        if not isinstance(node, dict) or node.get("jcr:primaryType") != "cq:Page":
            continue
        content = node.get("jcr:content") or {}
        agents.append(
            {
                "id": agent_id,
                "title": content.get("jcr:title", agent_id),
                "enabled": str(content.get("enabled", "false")).lower() == "true",
                "transport_uri": re.sub(r"//[^/@]+@", "//", str(content.get("transportUri", ""))) or None,
            }
        )
    return agents


def summarize_queue(
    agent_id: str, data: dict[str, Any], stuck_after_s: int = 300, now_ms: int | None = None
) -> dict[str, Any]:
    status = (data.get("metaData") or {}).get("queueStatus") or {}
    queue = data.get("queue") or []
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    times = [item.get("time") for item in queue if isinstance(item.get("time"), (int, float))]
    oldest_age_s = round((now_ms - min(times)) / 1000) if times else None
    head = queue[0] if queue else None

    if status.get("isBlocked"):
        state = "blocked"
    elif status.get("isPaused"):
        state = "paused"
    elif oldest_age_s is not None and oldest_age_s > stuck_after_s:
        state = "backlog"
    else:
        state = "ok"

    result: dict[str, Any] = {
        "agent": agent_id,
        "title": status.get("agentName", agent_id),
        "state": state,
        "queue_size": len(queue),
        "oldest_item_age_s": oldest_age_s,
    }
    if head:
        result["head_of_queue"] = {
            "path": head.get("path"),
            "type": head.get("type"),
            "user": head.get("userid"),
            "attempts": head.get("numProcessed", 0),
        }
        if head.get("numProcessed", 0) > 0:
            result["hint"] = (
                f"The first item has been retried {head['numProcessed']} times and is blocking the queue. Check the "
                "agent log and the target instance (credentials, disk space, receiving servlet). If the item itself "
                "is bad, clear only that item rather than the whole queue."
            )
    elif state == "paused":
        result["hint"] = "The queue is paused. Resume it from the agent page if this wasn't intentional."
    return result


# --- Oak indexes -----------------------------------------------------------------------------------------------------

INDEX_FIELDS = (
    "type",
    "async",
    "reindex",
    "reindexCount",
    "compatVersion",
    "includedPaths",
    "excludedPaths",
    "queryPaths",
    "tags",
    "selectionPolicy",
    "propertyNames",
    "declaringNodeTypes",
    "unique",
)


def summarize_indexes(
    data: dict[str, Any], name_contains: str | None = None, index_type: str | None = None
) -> dict[str, Any]:
    indexes = []
    warnings = []
    for name, node in sorted(data.items()):
        if not isinstance(node, dict) or node.get("jcr:primaryType") != "oak:QueryIndexDefinition":
            continue
        if not _matches(name_contains, name) or (index_type and node.get("type") != index_type):
            continue
        info = {"name": name, **{field: node[field] for field in INDEX_FIELDS if field in node}}
        indexes.append(info)
        if node.get("reindex") is True:
            warnings.append(
                f"{name}: reindex=true. Reindexing is still running or failed; check error.log for IndexUpdate "
                "messages before changing anything."
            )
        if node.get("type") == "lucene" and not node.get("async"):
            warnings.append(f"{name}: Lucene index without 'async'. Lucene indexes should be async ('async').")
        if node.get("type") == "disabled":
            warnings.append(f"{name}: index is disabled and won't be used by queries.")
    by_type = Counter(i.get("type", "unknown") for i in indexes)
    return {"count": len(indexes), "by_type": dict(by_type.most_common()), "warnings": warnings, "indexes": indexes}


# --- Logs ------------------------------------------------------------------------------------------------------------


def analyze_error_log_text(text: str, top: int = 10) -> dict[str, Any]:
    stats = ErrorLogStats()
    for entry in parse_error_log(text.splitlines(keepends=True)):
        stats.add(entry)
    summary = stats.to_dict(top)
    return {
        "entries": summary["entries"],
        "levels": summary["levels"],
        "window": {"start": summary["start"], "end": summary["end"]},
        "findings": build_findings(stats, None),
        "top_errors": summary["top_errors"],
        "traversal_queries": summary["traversal_queries"],
    }


def analyze_request_log_text(text: str, slow_ms: int = 1000, top: int = 10) -> dict[str, Any]:
    stats = RequestLogStats(slow_ms=slow_ms, top=top)
    for request in parse_request_log(text.splitlines(keepends=True)):
        stats.add(request)
    summary = stats.to_dict()
    summary.pop("requests_by_hour", None)
    summary["findings"] = build_findings(None, stats)
    return summary


# --- QueryBuilder ----------------------------------------------------------------------------------------------------

QUERY_ROOTS = ("/content", "/conf", "/apps", "/libs", "/etc", "/var")
_SAFE_PATH = re.compile(r"^(/[A-Za-z0-9_.:\-]+)+$")


def validate_query_path(path: str) -> str:
    """Queries must be scoped to a subtree like /content/<site> to avoid repository-wide traversals."""
    path = path.rstrip("/")
    if path and (not _SAFE_PATH.match(path) or ".." in path):
        raise ValueError(f"Invalid path {path!r}. Use an absolute JCR path such as /content/mysite.")
    if not path.startswith(QUERY_ROOTS) or path.count("/") < 2:
        raise ValueError(
            f"Path {path or '/'!r} is too broad. Scope the query to a subtree under one of {', '.join(QUERY_ROOTS)} "
            "(for example /content/mysite) to avoid repository traversal."
        )
    return path


def build_query_params(
    path: str,
    node_type: str | None = None,
    property_name: str | None = None,
    property_value: str | None = None,
    fulltext: str | None = None,
    order_by: str | None = None,
    limit: int = 20,
) -> dict[str, str]:
    params = {"path": validate_query_path(path), "p.limit": str(max(1, min(limit, 100))), "p.guessTotal": "true"}
    if node_type:
        params["type"] = node_type
    if property_name:
        params["property"] = property_name
        if property_value is not None:
            params["property.value"] = property_value
        else:
            params["property.operation"] = "exists"
    if fulltext:
        params["fulltext"] = fulltext
    if order_by:
        params["orderby"] = order_by if order_by.startswith("@") or order_by == "path" else f"@{order_by}"
        params["orderby.sort"] = "desc"
    return params


def summarize_query_result(params: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    hits = data.get("hits") or []
    return {
        "querybuilder_params": params,
        "returned": len(hits),
        "total_estimate": data.get("total"),
        "more": data.get("more", False),
        "hits": [
            {k: hit.get(k) for k in ("path", "title", "name", "lastModified") if hit.get(k) is not None} for hit in hits
        ],
        "tip": "Paste the params into /libs/cq/search/content/querydebug.html to see the Oak query and plan.",
    }
