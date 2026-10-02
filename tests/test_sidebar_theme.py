from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from onyx.app import create_app
from onyx.config import AppConfig
from onyx.markdown_theme import _UNSAFE
from onyx.sidebar_theme import ELEMENTS, folder_tints, stylesheet, validate_snapshot
from onyx.storage import Storage

PLUGIN_CAPTURE = Path(__file__).resolve().parent.parent / "integrations" / "obsidian" / "src" / "sidebar-theme.ts"


def snapshot(color: str = "rgb(7, 54, 66)", folder: str = "rgb(203, 75, 22)") -> dict:
    """A cream Solarized explorer in JetBrains Mono with a rainbow folder snippet."""
    return {
        "mode": "light",
        "styles": {
            "pane": {"background-color": "rgb(253, 246, 227)", "color": color, "font-family": '"JetBrains Mono", monospace'},
            "folder": {"color": folder, "font-size": "13px"},
            "file": {"color": color, "font-size": "13px", "padding-top": "4px"},
            "active": {"background-color": "rgb(238, 232, 213)", "color": color},
            "hover": {"background-color": "rgba(0, 0, 0, 0.05)"},
            "chevron": {"color": "rgb(147, 161, 161)"},
            "guide": {"border-left-color": "rgb(238, 232, 213)", "border-left-width": "1px", "border-left-style": "solid"},
            "search": {"border-radius": "999px", "background-color": "rgb(238, 232, 213)"},
        },
        "folders": [
            {"name": "Archive", "color": "rgb(220, 50, 47)"},
            {"name": "Inbox", "color": "rgb(203, 75, 22)", "guide": "rgb(203, 75, 22)", "hover": "rgba(203, 75, 22, 0.1)"},
            {"name": "Projects", "color": "rgb(42, 161, 152)"},
        ],
    }


class SidebarThemeTests(unittest.TestCase):
    def test_rejects_active_css_unknown_elements_and_bad_folders(self):
        for value in ("red;}body{display:none", "</style><script>alert(1)</script>",
                      "url(https://example.com)", r"u\72l(x)", "var(--external)", "red/*comment*/", "red\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_snapshot(snapshot(value))
            candidate = snapshot()
            candidate["folders"][0]["color"] = value
            with self.subTest(folder=value), self.assertRaises(ValueError):
                validate_snapshot(candidate)
        for bad in ({"styles": {"body": {"color": "red"}}}, {"styles": {"file": {"background-image": "none"}}},
                    {"folders": [{"name": "A", "color": "red", "icon": "x"}]}, {"folders": [{"name": "a\nb", "color": "red"}]},
                    {"folders": [{"name": "", "color": "red"}]}, {"mode": "sepia"}):
            candidate = snapshot()
            candidate.update({k: v for k, v in bad.items() if k != "styles"})
            if "styles" in bad:
                candidate["styles"] = {**candidate["styles"], **bad["styles"]}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_snapshot(candidate)
        missing_file = snapshot()
        del missing_file["styles"]["file"]
        with self.assertRaises(ValueError):
            validate_snapshot(missing_file)

    def test_stylesheet_switches_the_tree_to_obsidians_shape_under_its_scope(self):
        css = stylesheet(snapshot())
        rules = css.splitlines()
        self.assertTrue(all(rule.startswith("body.obsidian-tree ") for rule in rules), rules)
        self.assertIn("body.obsidian-tree #tree .fold{display:none} body.obsidian-tree #tree .chev{display:block}", css)
        # The pane, and chrome tokens in the pane's own mode: dark app, cream explorer, dark ink.
        self.assertIn("background:rgb(253, 246, 227)", css)
        self.assertIn("--ink:7 54 66", css)
        self.assertIn("color-scheme:light", css)
        # Folder and guide colours come per folder, with the captured ones as the fallback.
        self.assertIn("color:var(--folder-color,rgb(203, 75, 22))", css)
        self.assertIn("var(--guide-color,rgb(238, 232, 213))", css)
        # A folder row hovers in its own colour when the theme gives it one; files keep the plain hover.
        self.assertIn("body.obsidian-tree #tree summary:hover{background-color:var(--folder-hover,rgba(0, 0, 0, 0.05))}", css)
        self.assertIn("body.obsidian-tree #tree .file:hover{background-color:rgba(0, 0, 0, 0.05)}", css)
        self.assertIn('font-family:"JetBrains Mono", monospace', css)
        self.assertIn("body.obsidian-tree #vault-filter{background-color:rgb(238, 232, 213);border-radius:999px}", css)
        self.assertEqual(stylesheet(None), "")
        self.assertEqual([f["name"] for f in folder_tints(snapshot())], ["Archive", "Inbox", "Projects"])

    def test_plugin_capture_speaks_the_servers_allowlist(self):
        # One unsafe value sinks a whole snapshot, and a sync that is always refused
        # looks, from the sidebar, exactly like Obsidian being closed.
        source = PLUGIN_CAPTURE.read_text(encoding="utf-8")
        plugin_unsafe = re.search(r"const UNSAFE = /(.*)/i;", source).group(1).replace(r"\/", "/")
        self.assertEqual(plugin_unsafe, _UNSAFE.pattern)
        block = re.search(r"const ELEMENTS[^{]*\{(.*?)\n\};", source, re.DOTALL).group(1)
        self.assertEqual(set(re.findall(r"^\s+(\w+): \[", block, re.MULTILINE)) | {"hover"}, set(ELEMENTS))

    def test_api_persistence_setting_and_vault_page(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, other = root / "CX", root / "other"
            for folder in (vault / "Inbox", other):
                folder.mkdir(parents=True)
            (vault / "Inbox" / "note.md").write_text("# Note\n")
            config = AppConfig(default_folder=root, allowed_roots=(root,), port=8899, data_dir=root / "data")
            with TestClient(create_app(config), base_url="http://127.0.0.1:8899") as client:
                def settings(**patch):
                    self.assertEqual(client.post("/api/settings", json={"token": config.token, "settings": patch}).status_code, 200)

                settings(vault_root=str(vault))
                empty = client.get("/api/sidebar-theme").json()
                self.assertEqual((empty["available"], empty["css"], empty["folders"]), (False, "", []))
                self.assertIn('<body class="kind-notes">', client.get("/vault").text)  # Onyx's own sidebar until one arrives

                body = {"token": config.token, "vault_root": str(vault), "snapshot": snapshot()}
                self.assertEqual(client.post("/api/sidebar-theme", json={**body, "token": "wrong"}).status_code, 403)
                self.assertEqual(client.post("/api/sidebar-theme", json=body, headers={"Origin": "https://evil.test"}).status_code, 403)
                self.assertEqual(client.post("/api/sidebar-theme", content="x" * 150_000).status_code, 413)
                response = client.post("/api/sidebar-theme", json=body, headers={"Origin": "app://obsidian.md"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], "app://obsidian.md")
                theme = client.get("/api/sidebar-theme").json()
                self.assertTrue(theme["available"])
                self.assertEqual(theme["folders"][1], {"name": "Inbox", "color": "rgb(203, 75, 22)", "guide": "rgb(203, 75, 22)",
                                                       "hover": "rgba(203, 75, 22, 0.1)"})
                for page, kind in (("/vault", "notes"), ("/vault?vault=html", "html")):  # both sidebars, from the first paint
                    html = client.get(page).text
                    self.assertIn(f'<body class="kind-{kind} obsidian-tree">', html)
                    self.assertIn(f"<style id=sidebar-theme>{theme['css']}</style>", html)

                # Another vault's explorer never replaces the configured vault's.
                other_body = {**body, "vault_root": str(other), "snapshot": snapshot("rgb(1, 2, 3)")}
                self.assertEqual(client.post("/api/sidebar-theme", json=other_body).status_code, 200)
                self.assertEqual(client.get("/api/sidebar-theme").json()["revision"], theme["revision"])
                settings(sidebar_follow_obsidian=False)
                off = client.get("/api/sidebar-theme").json()
                self.assertEqual((off["enabled"], off["css"], off["folders"]), (False, "", []))
                self.assertIn('<body class="kind-notes">', client.get("/vault").text)
                settings(sidebar_follow_obsidian=True)
                self.assertTrue(client.get("/api/sidebar-theme").json()["last_sync"]["ok"])
                body["snapshot"] = snapshot("red;display:none")
                self.assertEqual(client.post("/api/sidebar-theme", json=body).status_code, 400)
                refused = client.get("/api/sidebar-theme").json()
                self.assertEqual(refused["revision"], theme["revision"])  # the good snapshot stays in force
                # A refusal is visible, not indistinguishable from Obsidian being closed…
                self.assertFalse(refused["last_sync"]["ok"])
                self.assertIn("unsafe", refused["last_sync"]["error"])
                # …but a caller without the token cannot write the status line.
                client.post("/api/sidebar-theme", json={**body, "token": "wrong", "snapshot": snapshot()})
                self.assertIn("unsafe", client.get("/api/sidebar-theme").json()["last_sync"]["error"])

            storage = Storage(root / "data")
            try:
                self.assertEqual(storage.sidebar_theme(vault), validate_snapshot(snapshot()))
                self.assertIsNone(storage.markdown_theme(vault))  # a separate kind of snapshot
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
