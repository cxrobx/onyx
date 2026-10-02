from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from onyx.app import create_app
from onyx.config import AppConfig
from onyx.markdown_theme import stylesheet, validate_snapshot
from onyx.storage import Storage


def snapshot(color: str = "rgb(196, 197, 181)") -> dict:
    return {"mode": "dark", "styles": {
        "content": {"color": color, "background-color": "rgb(26, 26, 26)",
                    "font-family": '"JetBrains Mono", monospace', "font-size": "18px", "max-width": "700px"},
        "h1": {"color": "rgb(88, 209, 235)", "font-size": "36px"},
        "code": {"background-color": "rgb(21, 21, 21)"},
    }}


class MarkdownThemeTests(unittest.TestCase):
    def test_rejects_active_css_and_unknown_selectors(self):
        for value in ("red;}body{display:none", "</style><script>alert(1)</script>",
                      "url(https://example.com)", r"u\72l(x)", "var(--external)", "red/*comment*/", "red\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_snapshot(snapshot(value))
        for key in ("body", "content, .askw-panel"):
            candidate = snapshot()
            candidate["styles"][key] = {"color": "red"}
            with self.assertRaises(ValueError):
                validate_snapshot(candidate)
        candidate = snapshot()
        candidate["styles"]["content"]["background-image"] = "none"
        with self.assertRaises(ValueError):
            validate_snapshot(candidate)
        # Notes, and the other pages Onyx lays out itself; an HTML page is authored and keeps its own look.
        self.assertIn(':is(body[data-askw-document-kind="markdown"],body[data-askw-document-kind="text"],'
                      'body[data-askw-document-kind="pdf"],body[data-askw-document-kind="selection"]) > main h1',
                      stylesheet(snapshot()))
        self.assertNotIn('document-kind="html"', stylesheet(snapshot()))

    def test_the_editor_takes_the_vault_lettering_but_not_its_block_spacing(self):
        candidate = snapshot()
        candidate["styles"]["h1"]["margin-top"] = "24px"
        css = stylesheet(candidate)
        heading = next(rule for rule in css.splitlines() if rule.endswith("{color:rgb(88, 209, 235);font-size:36px}"))
        self.assertTrue(heading.split("{")[0].endswith("> main .cm-line.askw-ed-h1"))
        self.assertIn("> main h1{color:rgb(88, 209, 235);font-size:36px;margin-top:24px}", css)  # the page keeps it
        self.assertIn("> main .askw-ed-code{background-color:rgb(21, 21, 21)}", css)

    def test_api_persistence_vault_selection_and_reading_pages_not_html(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            note = first / "note.md"
            note.write_text("# Heading\n\nText with `code`.")
            plain = first / "note.txt"
            plain.write_text("Plain text")
            html = first / "page.html"
            html.write_text("<html><body>Authored</body></html>")
            config = AppConfig(default_folder=root, allowed_roots=(root,), port=8899, data_dir=root / "data")
            app = create_app(config)
            with TestClient(app, base_url="http://127.0.0.1:8899") as client:
                def settings(**patch):
                    response = client.post("/api/settings", json={"token": config.token, "settings": patch})
                    self.assertEqual(response.status_code, 200)

                settings(vault_root=str(first))
                self.assertFalse(client.get("/api/markdown-theme").json()["available"])
                body = {"token": config.token, "vault_root": str(first), "snapshot": snapshot()}
                self.assertEqual(client.post("/api/markdown-theme", json={**body, "token": "wrong"}).status_code, 403)
                self.assertEqual(client.post("/api/markdown-theme", json=body, headers={"Origin": "https://evil.test"}).status_code, 403)
                self.assertEqual(client.post("/api/markdown-theme", json=[]).status_code, 400)
                self.assertEqual(client.post("/api/markdown-theme", content="x" * 150_000).status_code, 413)
                response = client.post("/api/markdown-theme", json=body, headers={"Origin": "app://obsidian.md"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], "app://obsidian.md")
                theme = client.get("/api/markdown-theme").json()
                self.assertTrue(theme["available"])
                self.assertIn("rgb(196, 197, 181)", theme["css"])
                rendered = client.get("/view", params={"src": str(note)}).text
                self.assertIn(theme["css"], rendered)
                # Onyx's own reading pages wear it too (a text file here); an authored HTML page never does.
                self.assertIn(theme["css"], client.get("/view", params={"src": str(plain)}).text)
                self.assertNotIn('id="askw-markdown-theme"', client.get("/view", params={"src": str(html)}).text)
                self.assertNotIn(theme["css"], client.get("/").text)

                # A second open vault cannot replace the selected vault's theme.
                body.update(vault_root=str(second), snapshot=snapshot("rgb(1, 2, 3)"))
                self.assertEqual(client.post("/api/markdown-theme", json=body).status_code, 200)
                self.assertEqual(client.get("/api/markdown-theme").json()["revision"], theme["revision"])
                settings(vault_root=str(second))
                self.assertIn("rgb(1, 2, 3)", client.get("/api/markdown-theme").json()["css"])
                settings(markdown_follow_obsidian=False)
                self.assertEqual(client.get("/api/markdown-theme").json()["css"], "")
                settings(markdown_follow_obsidian=True)
                body["snapshot"] = snapshot("red;display:none")
                self.assertEqual(client.post("/api/markdown-theme", json=body).status_code, 400)
                self.assertIn("rgb(1, 2, 3)", client.get("/api/markdown-theme").json()["css"])

            # Reopen the database with no Obsidian process or plugin connected.
            storage = Storage(root / "data")
            try:
                self.assertEqual(storage.markdown_theme(first), snapshot())
                self.assertEqual(storage.markdown_theme(second), snapshot("rgb(1, 2, 3)"))
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
