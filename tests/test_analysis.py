import unittest

from aem_mcp import analysis
from aem_mcp.mock_aem import BUNDLES, COMPONENTS, INDEXES, _agents, _log_lines, _queue


class BundleTests(unittest.TestCase):
    def test_flags_installed_and_resolved_but_not_fragments(self):
        result = analysis.summarize_bundles({"status": "x", "data": BUNDLES})
        states = {b["symbolic_name"]: b["state"] for b in result["bundles"]}
        self.assertEqual(
            states, {"com.mysite.integrations.weather": "Installed", "com.example.legacy-tagging": "Resolved"}
        )
        self.assertEqual(result["problem_count"], 2)
        self.assertIn("Installed", result["hints"])

    def test_include_all_with_filter(self):
        result = analysis.summarize_bundles({"data": BUNDLES}, include_all=True, name_contains="SLING")
        self.assertTrue(all("sling" in b["symbolic_name"] for b in result["bundles"]))
        self.assertEqual(len(result["bundles"]), 4)

    def test_limit_truncates(self):
        result = analysis.summarize_bundles({"data": BUNDLES}, include_all=True, limit=3)
        self.assertEqual(len(result["bundles"]), 3)
        self.assertTrue(result["truncated"])


class ComponentTests(unittest.TestCase):
    def test_problem_states(self):
        result = analysis.summarize_components({"data": COMPONENTS})
        names = {c["name"].rsplit(".", 1)[-1]: c["state"] for c in result["problems"]}
        self.assertEqual(
            names,
            {"TagSyncServiceImpl": "unsatisfied (reference)", "ContentSyncScheduler": "failed activation"},
        )
        self.assertEqual(set(result["hints"]), {"unsatisfied (reference)", "failed activation"})


class ReplicationTests(unittest.TestCase):
    def test_agents_never_expose_credentials(self):
        agents = analysis.list_replication_agents(_agents())
        self.assertEqual([a["id"] for a in agents], ["publish", "publish2", "test_and_target"])
        self.assertFalse(agents[2]["enabled"])
        flat = repr(agents)
        self.assertNotIn("transportPassword", flat)
        self.assertNotIn("replication-receiver", flat)

    def test_transport_uri_userinfo_is_stripped(self):
        data = {"a": {"jcr:primaryType": "cq:Page", "jcr:content": {"transportUri": "http://u:p@host/bin/receive"}}}
        self.assertEqual(analysis.list_replication_agents(data)[0]["transport_uri"], "http://host/bin/receive")

    def test_blocked_queue_identifies_head_item(self):
        result = analysis.summarize_queue("publish", _queue("publish"))
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["queue_size"], 37)
        self.assertEqual(result["head_of_queue"]["attempts"], 14)
        self.assertGreater(result["oldest_item_age_s"], 2700)
        self.assertIn("retried 14 times", result["hint"])

    def test_empty_queue_ok(self):
        self.assertEqual(analysis.summarize_queue("publish2", _queue("publish2"))["state"], "ok")

    def test_backlog_without_block(self):
        data = {"metaData": {"queueStatus": {}}, "queue": [{"time": 0, "path": "/content/a", "numProcessed": 0}]}
        result = analysis.summarize_queue("p", data, stuck_after_s=60, now_ms=120_000)
        self.assertEqual(result["state"], "backlog")
        self.assertNotIn("hint", result)


class IndexTests(unittest.TestCase):
    def test_summary_and_warnings(self):
        result = analysis.summarize_indexes(INDEXES)
        self.assertEqual(result["count"], 7)
        self.assertEqual(result["by_type"]["lucene"], 4)
        joined = "\n".join(result["warnings"])
        self.assertIn("mysiteArticleIndex-custom-1: reindex=true", joined)
        self.assertIn("mysiteLegacyIndex: Lucene index without 'async'", joined)
        self.assertIn("commerceLucene: index is disabled", joined)

    def test_filters(self):
        self.assertEqual(analysis.summarize_indexes(INDEXES, index_type="property")["count"], 2)
        self.assertEqual(analysis.summarize_indexes(INDEXES, name_contains="DAM")["count"], 1)


class LogTests(unittest.TestCase):
    def test_error_log_findings(self):
        result = analysis.analyze_error_log_text("".join(_log_lines("error.log")))
        rules = {f["rule"] for f in result["findings"]}
        self.assertTrue({"jvm-oom", "oak-traversal", "unclosed-resolver", "service-user-mapping"} <= rules)
        self.assertEqual(result["findings"][0]["severity"], "critical")

    def test_request_log_stats(self):
        result = analysis.analyze_request_log_text("".join(_log_lines("request.log")))
        self.assertEqual(result["latency_ms"]["p50"], 45)
        self.assertIn("http-5xx", {f["rule"] for f in result["findings"]})
        self.assertNotIn("requests_by_hour", result)


class QueryGuardrailTests(unittest.TestCase):
    def test_rejects_broad_or_unsafe_paths(self):
        for path in ("/", "/content", "/home/users", "content/mysite", "/content/../home", "/content/a b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                analysis.validate_query_path(path)

    def test_builds_params_and_caps_limit(self):
        params = analysis.build_query_params(
            "/content/mysite/",
            node_type="cq:Page",
            property_name="jcr:content/cq:template",
            limit=500,
            order_by="jcr:content/cq:lastModified",
        )
        self.assertEqual(params["path"], "/content/mysite")
        self.assertEqual(params["p.limit"], "100")
        self.assertEqual(params["p.guessTotal"], "true")
        self.assertEqual(params["property.operation"], "exists")
        self.assertEqual(params["orderby"], "@jcr:content/cq:lastModified")


if __name__ == "__main__":
    unittest.main()
