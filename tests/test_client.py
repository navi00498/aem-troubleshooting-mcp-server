import socket
import unittest

from aem_mcp.client import AemClient, AemConfig, AemError
from aem_mcp.mock_aem import MockAem


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ConfigTests(unittest.TestCase):
    def test_from_env(self):
        config = AemConfig.from_env(
            {"AEM_URL": "https://author.example.com/", "AEM_USER": "u", "AEM_PASSWORD": "p", "AEM_VERIFY_TLS": "false"}
        )
        self.assertEqual(config.url, "https://author.example.com")
        self.assertFalse(config.verify_tls)
        self.assertEqual(config.describe(), {"url": config.url, "auth": "basic", "user": "u", "verify_tls": False})
        self.assertNotIn("p", config.describe().values())

    def test_token_wins(self):
        client = AemClient(AemConfig(user="u", password="p", token="t"))
        self.assertEqual(client._headers("*/*")["Authorization"], "Bearer t")

    def test_url_quoting(self):
        client = AemClient(AemConfig(url="http://x"))
        self.assertEqual(client.url_for("/oak:index.1.json"), "http://x/oak:index.1.json")
        self.assertEqual(client.url_for("/a b", {"q": "x y", "skip": None}), "http://x/a%20b?q=x+y")
        with self.assertRaises(ValueError):
            client.url_for("relative")


class ClientAgainstMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockAem(credentials=("admin", "secret")).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.mock.__exit__(None, None, None)

    def test_authenticated_json(self):
        client = AemClient(AemConfig(url=self.mock.url, user="admin", password="secret"))
        self.assertIn("Bundle information", client.get_json("/system/console/bundles.json")["status"])

    def test_401_message(self):
        client = AemClient(AemConfig(url=self.mock.url, user="admin", password="wrong"))
        with self.assertRaises(AemError) as ctx:
            client.get_json("/system/console/bundles.json")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("AEM_USER", str(ctx.exception))
        self.assertNotIn("wrong", str(ctx.exception))

    def test_login_page_detected(self):
        client = AemClient(AemConfig(url=self.mock.url))
        with self.assertRaises(AemError) as ctx:
            client.get_json("/oak:index.1.json")
        self.assertIn("login page", str(ctx.exception))

    def test_404_message(self):
        client = AemClient(AemConfig(url=self.mock.url, user="admin", password="secret"))
        with self.assertRaises(AemError) as ctx:
            client.get_json("/does/not/exist.json")
        self.assertEqual(ctx.exception.status, 404)

    def test_unreachable(self):
        client = AemClient(AemConfig(url=f"http://127.0.0.1:{_free_port()}", timeout=2))
        with self.assertRaises(AemError) as ctx:
            client.get_json("/system/console/bundles.json")
        self.assertIn("Cannot reach AEM", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
