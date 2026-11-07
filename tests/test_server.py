import json
import socket
import unittest

from mcp.client.client import Client

from aem_mcp.client import AemConfig
from aem_mcp.mock_aem import MockAem
from aem_mcp.server import build_server

EXPECTED_TOOLS = {
    "aem_connection_info",
    "aem_health_summary",
    "list_bundles",
    "list_component_problems",
    "check_replication_queues",
    "tail_log",
    "analyze_error_log",
    "analyze_request_log",
    "list_oak_indexes",
    "get_index_definition",
    "query_content",
}


def _payload(result):
    return json.loads(result.content[0].text)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockAem().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.mock.__exit__(None, None, None)

    async def call(self, name, args=None, config=None):
        server = build_server(config or AemConfig(url=self.mock.url))
        async with Client(server) as client:
            return await client.call_tool(name, args or {})

    async def test_all_tools_registered_and_read_only(self):
        async with Client(build_server(AemConfig(url=self.mock.url))) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertTrue(tool.description)

    async def test_health_summary_finds_planted_problems(self):
        result = await self.call("aem_health_summary")
        self.assertFalse(result.is_error)
        data = _payload(result)
        self.assertEqual(data["overall"], "critical")
        problems = "\n".join(data["problems"])
        self.assertIn("2 bundle(s) not active", problems)
        self.assertIn("2 component(s) unsatisfied or failed", problems)
        self.assertIn("replication queue 'publish' is blocked (37)", problems)
        self.assertIn("JVM OutOfMemoryError", problems)

    async def test_health_summary_degrades_gracefully(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = await self.call("aem_health_summary", config=AemConfig(url=f"http://127.0.0.1:{port}", timeout=2))
        data = _payload(result)
        self.assertFalse(result.is_error)
        self.assertIn("unavailable", data["bundles"])
        self.assertEqual(data["overall"], "healthy")

    async def test_connection_info_hides_password(self):
        config = AemConfig(url=self.mock.url, user="admin", password="hunter2")
        result = await self.call("aem_connection_info", config=config)
        self.assertNotIn("hunter2", result.content[0].text)
        self.assertTrue(_payload(result)["reachable"])

    async def test_tail_log_with_grep(self):
        result = await self.call("tail_log", {"lines": 5, "grep": "OutOfMemoryError"})
        lines = result.content[0].text.splitlines()
        self.assertTrue(1 <= len(lines) <= 5)
        self.assertTrue(all("outofmemoryerror" in line.lower() for line in lines))

    async def test_analyze_logs(self):
        errors = _payload(await self.call("analyze_error_log", {"lines": 20000}))
        self.assertTrue(errors["traversal_queries"])
        requests = _payload(await self.call("analyze_request_log", {"lines": 20000}))
        self.assertEqual(requests["latency_ms"]["p99"], 3627)

    async def test_unknown_log_is_clear_error(self):
        result = await self.call("tail_log", {"log_name": "/logs/nope.log"})
        self.assertTrue(result.is_error)
        self.assertIn("404", result.content[0].text)

    async def test_index_tools(self):
        data = _payload(await self.call("list_oak_indexes", {"index_type": "lucene"}))
        self.assertEqual(data["count"], 4)
        definition = _payload(await self.call("get_index_definition", {"name": "cqPageLucene"}))
        self.assertIn("cq:Page", definition["indexRules"])
        bad = await self.call("get_index_definition", {"name": "../../etc"})
        self.assertTrue(bad.is_error)

    async def test_query_content(self):
        data = _payload(
            await self.call(
                "query_content",
                {"path": "/content/mysite", "node_type": "cq:Page", "fulltext": "blog post", "limit": 5},
            )
        )
        self.assertEqual(data["returned"], 5)
        self.assertTrue(data["more"])
        self.assertTrue(all(h["path"].startswith("/content/mysite/") for h in data["hits"]))

    async def test_query_guardrail_surfaces_as_tool_error(self):
        result = await self.call("query_content", {"path": "/"})
        self.assertTrue(result.is_error)
        self.assertIn("too broad", result.content[0].text)

    async def test_triage_prompt(self):
        async with Client(build_server(AemConfig(url=self.mock.url))) as client:
            prompt = await client.get_prompt("triage_aem_incident", {"symptom": "pages publish slowly"})
        self.assertIn("pages publish slowly", prompt.messages[0].content.text)


if __name__ == "__main__":
    unittest.main()
