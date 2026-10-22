"""Verify browser asset URLs against the real app routes, without lifespan."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import unittest

from tests.http_helpers import ROOT, asgi_request


class ScriptSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.sources.append(attributes["src"])


class FrontendAssetTests(unittest.TestCase):
    def test_homepage_scripts_load_from_real_mounted_routes(self):
        from backend.main import app

        page = asgi_request(app, "GET", "/static/index.html")
        self.assertEqual(page.status_code, 200)
        parser = ScriptSources()
        parser.feed(page.text)
        self.assertTrue(parser.sources, "An empty script inventory must not pass")
        local_sources = []
        for source in parser.sources:
            resolved = urlsplit(urljoin("http://testserver/static/index.html", source))
            if resolved.netloc != "testserver":
                continue
            local_sources.append(resolved.path)
            with self.subTest(script=source):
                response = asgi_request(app, "GET", resolved.path)
                self.assertEqual(response.status_code, 200, source)
                self.assertIn("javascript", response.headers.get("content-type", ""))
                self.assertEqual(response.content, (ROOT / resolved.path.lstrip("/")).read_bytes())
        self.assertIn("/static/previous_minutes.js", local_sources)
        self.assertIn("/static/previous_minutes_rerun.js", local_sources)

    def test_previous_minutes_modules_follow_their_shared_dependencies(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        upload = html.index('src="/static/previous_minutes.js"')
        rerun = html.index('src="/static/previous_minutes_rerun.js"')
        self.assertLess(html.index("let runtimeConfig"), upload)
        self.assertLess(html.index("function formatBytes("), upload)
        self.assertLess(upload, rerun)


if __name__ == "__main__":
    unittest.main()
