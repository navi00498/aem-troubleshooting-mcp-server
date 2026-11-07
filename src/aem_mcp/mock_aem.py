"""A small fake AEM author instance for demos and tests.

It serves the same read-only endpoints the MCP server uses, with realistic problems planted:
an unresolvable bundle, unsatisfied and failed DS components, a blocked replication queue,
a stuck reindex, and the synthetic error.log/request.log from aem-log-analyzer.

    aem-mock-server --port 4502            # then: AEM_URL=http://localhost:4502 aem-troubleshooting-mcp
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

LOGIN_PAGE = b"<!DOCTYPE html><html><head><title>AEM Sign In</title></head><body>Sign in</body></html>"


def _bundle(bid: int, sym: str, name: str, version: str, state: str = "Active") -> dict[str, Any]:
    raw = {"Active": 32, "Resolved": 4, "Installed": 2, "Fragment": 4}[state]
    return {
        "id": bid,
        "name": name,
        "fragment": state == "Fragment",
        "stateRaw": raw,
        "state": state,
        "version": version,
        "symbolicName": sym,
        "category": "",
    }


BUNDLES = [
    _bundle(0, "org.apache.felix.framework", "System Bundle", "7.0.5"),
    _bundle(12, "org.apache.sling.api", "Apache Sling API", "2.27.6"),
    _bundle(14, "org.apache.sling.engine", "Apache Sling Engine Implementation", "2.15.12"),
    _bundle(20, "org.apache.sling.commons.log", "Apache Sling Commons Log", "5.5.0"),
    _bundle(31, "org.apache.jackrabbit.oak-core", "Oak Core", "1.68.0"),
    _bundle(32, "org.apache.jackrabbit.oak-lucene", "Oak Lucene", "1.68.0"),
    _bundle(40, "com.adobe.granite.replication.core", "Adobe Granite Replication Core", "6.4.30"),
    _bundle(41, "com.day.cq.wcm.cq-wcm-core", "Day Communique 5 WCM Core Implementation", "5.13.120"),
    _bundle(55, "com.fasterxml.jackson.core.jackson-databind", "Jackson-databind", "2.16.1"),
    _bundle(
        60,
        "org.apache.sling.extensions.webconsolesecurityprovider",
        "Web Console Security Provider",
        "1.2.8",
        "Fragment",
    ),
    _bundle(601, "com.mysite.core", "My Site - Core", "1.4.2"),
    _bundle(602, "com.mysite.integrations.weather", "My Site - Weather Integration", "1.1.0", "Installed"),
    _bundle(603, "com.example.legacy-tagging", "Legacy Tagging Utilities", "0.9.3", "Resolved"),
]


def _component(cid: int | str, name: str, state: str, bundle_id: int = 601) -> dict[str, Any]:
    raw = {
        "active": 8,
        "satisfied": 4,
        "unsatisfied (reference)": 2,
        "unsatisfied (configuration)": 1,
        "failed activation": 16,
        "disabled": -1,
        "no config": -1,
    }[state]
    return {
        "id": str(cid) if raw != -1 else "",
        "bundleId": bundle_id,
        "name": name,
        "state": state,
        "stateRaw": raw,
        "pid": name,
    }


COMPONENTS = [
    _component(101, "org.apache.sling.engine.impl.SlingMainServlet", "active", 14),
    _component(140, "com.day.cq.replication.impl.ReplicatorImpl", "active", 40),
    _component(901, "com.mysite.core.models.impl.ArticleModelImpl", "active"),
    _component(902, "com.mysite.core.servlets.SearchServlet", "active"),
    _component(903, "com.mysite.core.services.impl.TagSyncServiceImpl", "unsatisfied (reference)"),
    _component(904, "com.mysite.core.schedulers.ContentSyncScheduler", "failed activation"),
    _component(905, "com.mysite.core.services.impl.CdnPurgeServiceImpl", "satisfied"),
    _component("", "com.mysite.core.services.impl.SalesforceConnectorImpl", "no config"),
]


def _agents() -> dict[str, Any]:
    def agent(title: str, enabled: bool, uri: str) -> dict[str, Any]:
        return {
            "jcr:primaryType": "cq:Page",
            "jcr:content": {
                "jcr:primaryType": "nt:unstructured",
                "jcr:title": title,
                "enabled": "true" if enabled else "false",
                "transportUri": uri,
                "transportUser": "replication-receiver",
                "transportPassword": "{36b0dd8f0b4d2bd0cbe6a0fcd9ee3f9d}",
            },
        }

    return {
        "jcr:primaryType": "cq:Page",
        "jcr:created": "Mon Nov 02 2026 08:00:00 GMT+0000",
        "publish": agent("Default Agent (publish)", True, "http://publish:4503/bin/receive?sling:authRequestLogin=1"),
        "publish2": agent("Publish 2", True, "http://publish2:4503/bin/receive?sling:authRequestLogin=1"),
        "test_and_target": agent("Test and Target (Adobe Target)", False, "tnt:///"),
    }


def _queue(agent_id: str) -> dict[str, Any]:
    now = int(time.time() * 1000)
    fields = ["id", "path", "time", "userid", "type", "size", "lastProcessed", "numProcessed"]
    if agent_id != "publish":
        status = {
            "agentName": agent_id,
            "agentId": agent_id,
            "isBlocked": False,
            "isPaused": False,
            "time": now,
            "processingSince": None,
            "lastProcessTime": now - 4000,
            "nextRetryPeriod": 0,
        }
        return {"metaData": {"root": "queue", "queueStatus": status, "fields": fields}, "queue": []}
    queue = []
    for i in range(37):
        created = now - (47 * 60 - i * 20) * 1000
        queue.append(
            {
                "id": f"2026/11/2/13/0/{i:04d}-replication-job",
                "path": f"/content/mysite/us/en/blog/post-{i + 20}",
                "time": created,
                "userid": "content-author",
                "type": "Activate",
                "size": 18432,
                "lastProcessed": now - 30_000 if i == 0 else 0,
                "numProcessed": 14 if i == 0 else 0,
            }
        )
    status = {
        "agentName": "Default Agent (publish)",
        "agentId": "publish",
        "isBlocked": True,
        "isPaused": False,
        "time": now,
        "processingSince": now - 47 * 60 * 1000,
        "lastProcessTime": now - 30_000,
        "nextRetryPeriod": 30_000,
    }
    return {"metaData": {"root": "queue", "queueStatus": status, "fields": fields}, "queue": queue}


def _index(type_: str, **props: Any) -> dict[str, Any]:
    return {"jcr:primaryType": "oak:QueryIndexDefinition", "type": type_, **props}


INDEXES = {
    "jcr:primaryType": "nt:unstructured",
    "uuid": _index("property", propertyNames=["jcr:uuid"], unique=True, reindex=False, reindexCount=1),
    "nodetype": _index("property", propertyNames=["jcr:primaryType", "jcr:mixinTypes"], reindex=False, reindexCount=1),
    "cqPageLucene": _index(
        "lucene",
        **{
            "async": ["async", "nrt"],
            "compatVersion": 2,
            "reindex": False,
            "reindexCount": 2,
            "evaluatePathRestrictions": True,
        },
    ),
    "damAssetLucene": _index(
        "lucene",
        **{
            "async": ["async", "nrt"],
            "compatVersion": 2,
            "reindex": False,
            "reindexCount": 4,
            "includedPaths": ["/content/dam"],
        },
    ),
    "mysiteArticleIndex-custom-1": _index(
        "lucene",
        **{
            "async": "async",
            "compatVersion": 2,
            "reindex": True,
            "reindexCount": 3,
            "includedPaths": ["/content/mysite"],
            "tags": ["mysite"],
            "selectionPolicy": "tag",
        },
    ),
    "mysiteLegacyIndex": _index("lucene", compatVersion=1, reindex=False, reindexCount=1),
    "commerceLucene": _index("disabled", **{"async": "async", "reindex": False, "reindexCount": 1}),
}

INDEX_DEFINITIONS = {
    "cqPageLucene": {
        **INDEXES["cqPageLucene"],
        "indexRules": {
            "jcr:primaryType": "nt:unstructured",
            "cq:Page": {
                "jcr:primaryType": "nt:unstructured",
                "properties": {
                    "jcr:primaryType": "nt:unstructured",
                    "cqLastModified": {"name": "jcr:content/cq:lastModified", "ordered": True, "propertyIndex": True},
                    "cqTemplate": {"name": "jcr:content/cq:template", "propertyIndex": True},
                    "jcrTitle": {"name": "jcr:content/jcr:title", "propertyIndex": True, "nodeScopeIndex": True},
                },
            },
        },
    },
}

CONTENT = (
    [
        {
            "path": f"/content/mysite/us/en/blog/post-{i}",
            "name": f"post-{i}",
            "title": f"Blog post {i}",
            "type": "cq:Page",
            "lastModified": f"2026-10-{(i % 28) + 1:02d}T10:00:00.000Z",
        }
        for i in range(1, 61)
    ]
    + [
        {
            "path": "/content/mysite/us/en",
            "name": "en",
            "title": "English",
            "type": "cq:Page",
            "lastModified": "2026-09-01T10:00:00.000Z",
        },
        {
            "path": "/content/mysite/us/en/search",
            "name": "search",
            "title": "Search",
            "type": "cq:Page",
            "lastModified": "2026-10-30T10:00:00.000Z",
        },
    ]
    + [
        {
            "path": f"/content/dam/mysite/images/hero-{i}.jpg",
            "name": f"hero-{i}.jpg",
            "title": f"Hero image {i}",
            "type": "dam:Asset",
            "lastModified": "2026-10-15T10:00:00.000Z",
        }
        for i in range(1, 11)
    ]
)


@lru_cache(maxsize=4)
def _log_lines(name: str) -> tuple[str, ...]:
    data = resources.files("aem_mcp").joinpath("mock_data", f"{name}.gz").read_bytes()
    return tuple(gzip.decompress(data).decode("utf-8").splitlines(keepends=True))


def _tail(params: dict[str, str]) -> str | None:
    name = params.get("name", "/logs/error.log")
    log = {"/logs/error.log": "error.log", "/logs/request.log": "request.log"}.get(name)
    if log is None:
        return None
    lines: Any = _log_lines(log)
    grep = params.get("grep")
    if grep and grep != "*":
        try:
            pattern = re.compile(grep, re.IGNORECASE)
            lines = [line for line in lines if pattern.search(line)]
        except re.error:
            lines = [line for line in lines if grep.lower() in line.lower()]
    tail = int(params.get("tail", "1000"))
    return "".join(lines[-tail:] if tail >= 0 else lines)


def _query(params: dict[str, str]) -> dict[str, Any]:
    path = params.get("path", "/")
    hits = [c for c in CONTENT if c["path"] == path or c["path"].startswith(path.rstrip("/") + "/")]
    if params.get("type"):
        hits = [c for c in hits if c["type"] == params["type"]]
    if params.get("fulltext"):
        hits = [c for c in hits if params["fulltext"].lower() in c["title"].lower()]
    if params.get("orderby"):
        hits.sort(key=lambda c: c["lastModified"], reverse=params.get("orderby.sort") == "desc")
    limit = int(params.get("p.limit", "10"))
    page = hits[:limit]
    return {
        "success": True,
        "results": len(page),
        "total": len(hits),
        "more": len(hits) > limit,
        "offset": 0,
        "hits": [{k: v for k, v in c.items() if k != "type"} | {"excerpt": ""} for c in page],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockAEM/1.0"
    credentials: tuple[str, str] | None = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from BaseHTTPRequestHandler
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any) -> None:
        self._send(200, json.dumps(payload).encode())

    def _authorized(self) -> bool:
        if self.credentials is None:
            return True
        expected = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        url = urlsplit(self.path)
        path = unquote(url.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}

        if not self._authorized():
            if path.startswith("/system/console"):
                self._send(401, b"Unauthorized", "text/plain")
            else:
                self._send(200, LOGIN_PAGE, "text/html")  # real AEM redirects to the login page
            return

        if path == "/system/console/bundles.json":
            states = [b["state"] for b in BUNDLES]
            counts = [
                len(BUNDLES),
                states.count("Active"),
                states.count("Fragment"),
                states.count("Resolved"),
                states.count("Installed"),
            ]
            status = (
                f"Bundle information: {counts[0]} bundles in total, {counts[1]} bundles active, "
                f"{counts[2]} bundles active fragments, {counts[3]} bundles resolved, "
                f"{counts[4]} bundles installed."
            )
            self._json({"status": status, "s": counts, "data": BUNDLES})
        elif path == "/system/console/components.json":
            self._json({"status": len(COMPONENTS), "data": COMPONENTS})
        elif path == "/system/console/slinglog/tailer.txt":
            text = _tail(params)
            if text is None:
                self._send(404, b"Unknown appender", "text/plain")
            else:
                self._send(200, text.encode(), "text/plain; charset=utf-8")
        elif path == "/etc/replication/agents.author.2.json":
            self._json(_agents())
        elif m := re.fullmatch(r"/etc/replication/agents\.author/([\w-]+)/jcr:content\.queue\.json", path):
            if m[1] in _agents():
                self._json(_queue(m[1]))
            else:
                self._send(404, b"Not found", "text/plain")
        elif path == "/oak:index.1.json":
            self._json(INDEXES)
        elif m := re.fullmatch(r"/oak:index/([\w.\-]+)\.(?:infinity|\d+)\.json", path):
            if m[1] in INDEX_DEFINITIONS:
                self._json(INDEX_DEFINITIONS[m[1]])
            elif m[1] in INDEXES:
                self._json(INDEXES[m[1]])
            else:
                self._send(404, b"Not found", "text/plain")
        elif path == "/bin/querybuilder.json":
            self._json(_query(params))
        else:
            self._send(404, b"Not found", "text/plain")


class MockAem:
    """Run the mock in a background thread: ``with MockAem() as aem: aem.url``."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, credentials: tuple[str, str] | None = None) -> None:
        handler = type("Handler", (_Handler,), {"credentials": credentials})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> MockAem:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aem-mock-server", description="Fake AEM author instance for demos.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4502)
    parser.add_argument("--user", help="require basic auth with this user (password from --password)")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args(argv)
    credentials = (args.user, args.password) if args.user else None
    handler = type("Handler", (_Handler,), {"credentials": credentials})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Mock AEM listening on http://{args.host}:{args.port} (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
