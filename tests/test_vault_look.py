from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from onyx.app import create_app
from onyx.config import AppConfig
from onyx.vault_look import SHADES, contrast, palette, reader_stylesheet, stylesheet

FONT = '"JetBrains Mono", Inter, ui-sans-serif, -apple-system, sans-serif'


def solarized() -> tuple[dict, dict]:
    """The vault as the plugin measured it: Solarized Light in JetBrains Mono, with orange links."""
    markdown = {"mode": "light", "styles": {
        "content": {"background-color": "rgb(253, 246, 227)", "color": "rgb(0, 43, 54)", "font-family": FONT},
        "a": {"color": "rgb(203, 75, 22)"},
        "code": {"background-color": "rgb(224, 215, 184)"},
        "pre": {"background-color": "rgb(224, 215, 184)"},
    }}
    sidebar = {"mode": "light", "styles": {
        "pane": {"background-color": "rgb(253, 246, 227)", "color": "rgb(0, 43, 54)", "font-family": FONT},
        "file": {"color": "rgb(77, 96, 102)"},
        "search": {"background-color": "rgba(224, 215, 184, 0.3)"},
        "active": {"background-color": "rgba(0, 43, 54, 0.075)"},
    }}
    return markdown, sidebar


def dark() -> tuple[dict, None]:
    return {"mode": "dark", "styles": {
        "content": {"background-color": "rgb(30, 30, 30)", "color": "rgb(218, 218, 218)", "font-family": "Inter, sans-serif"},
        "a": {"color": "rgb(138, 92, 245)"},
        "code": {"background-color": "rgb(45, 45, 45)"},
    }}, None


def rgb(triplet: str) -> tuple[float, float, float]:
    return tuple(float(v) for v in triplet.split())  # type: ignore[return-value]


class VaultLookTests(unittest.TestCase):
    def test_the_app_takes_the_vaults_ground_ink_accent_and_font(self):
        look = palette(*solarized())
        tokens = look["tokens"]
        self.assertEqual((look["mode"], look["base"]), ("light", [253, 246, 227]))
        self.assertEqual({tokens[k] for k in ("--bg-primary", "--bg-sidebar", "--bg-elevated")}, {"253 246 227"})
        self.assertEqual(tokens["--ink"], "0 43 54")
        self.assertEqual(tokens["--accent"], "203 75 22")  # the orange links, 4.2:1 on the cream
        # A light vault's primary button is its ink, like the app's own near-black one.
        self.assertEqual((tokens["--button-bg"], tokens["--button-ink"]), ("0 43 54", "253 246 227"))
        # Cards sit 40% of the way to the code-block ground; the search field is Obsidian's own, composited.
        self.assertEqual(tokens["--bg-surface"], "241 234 210")
        self.assertEqual(tokens["--bg-input"], "244 237 214")
        self.assertEqual(tokens["--ui-font"], FONT)

    def test_text_shades_keep_the_apps_contrasts_whatever_the_theme(self):
        for snapshots in (solarized(), dark()):
            tokens = palette(*snapshots)["tokens"]
            ground, louder = rgb(tokens["--bg-primary"]), rgb(tokens["--ink"])
            for name, target in SHADES.items():
                shade = rgb(tokens[f"--{name}"])
                with self.subTest(theme=snapshots[0]["mode"], shade=name):
                    self.assertGreaterEqual(contrast(shade, ground), target)
                    self.assertLess(contrast(shade, ground), contrast(louder, ground))  # quieter than the one before
                louder = shade

    def test_a_dark_vault_raises_its_buttons_and_dialogs_off_the_ground(self):
        look = palette(*dark())
        tokens = look["tokens"]
        self.assertEqual(look["mode"], "dark")
        self.assertEqual(tokens["--accent"], "138 92 245")
        self.assertEqual(tokens["--button-ink"], tokens["--ink"])
        for key in ("--button-bg", "--bg-elevated"):
            self.assertGreater(sum(rgb(tokens[key])), sum(rgb(tokens["--bg-primary"])), key)

    def test_a_faint_accent_falls_back_to_the_ink(self):
        markdown, sidebar = solarized()
        markdown["styles"]["a"]["color"] = "rgb(250, 240, 215)"
        self.assertEqual(palette(markdown, sidebar)["tokens"]["--accent"], "0 43 54")

    def test_no_look_without_an_opaque_ground_and_readable_ink(self):
        markdown, sidebar = solarized()
        self.assertIsNone(palette(None, None))
        for ground in ("rgba(0, 0, 0, 0)", "oklch(97% 0.02 90)"):
            candidate = {"mode": "light", "styles": {"content": {"background-color": ground, "color": "rgb(0, 43, 54)"}}}
            self.assertIsNone(palette(candidate, None), ground)
        markdown["styles"]["content"]["color"] = "rgb(240, 232, 212)"  # cream on cream
        self.assertIsNone(palette(markdown, None))
        # The explorer alone is enough: its pane gives the ground and the ink.
        self.assertEqual(palette(None, sidebar)["tokens"]["--ink"], "0 43 54")

    def test_the_chrome_takes_the_interface_font_never_the_reading_font(self):
        markdown, _sidebar = solarized()
        markdown["styles"]["content"]["font-family"] = "Georgia, serif"
        self.assertNotIn("--ui-font", palette(markdown, None)["tokens"])

    def test_only_numbers_and_a_checked_font_reach_the_css(self):
        markdown, sidebar = solarized()
        for font in ("x;}body{display:none", "url(https://example.com/f.woff)", "var(--external)"):
            sidebar["styles"]["pane"]["font-family"] = font
            markdown["styles"]["content"]["font-family"] = font
            self.assertNotIn("--ui-font", palette(markdown, sidebar)["tokens"], font)
        css = stylesheet(palette(*solarized()))
        self.assertTrue(css.startswith(":root.vault-look{--bg-primary:253 246 227;"))
        self.assertIn("color-scheme:light", css)
        self.assertIn(":root.vault-look body.obsidian-tree aside{", css)
        without_font = re.sub(r"--ui-font:[^;]*;", "", css)
        self.assertNotRegex(without_font, r"url\(|var\(|[{}]\s*[a-z-]+\s*\{")
        self.assertEqual(stylesheet(None), "")

    def test_the_answer_panel_takes_the_same_palette(self):
        css = reader_stylesheet(palette(*solarized()))
        self.assertTrue(css.startswith("html[data-askw-look] .askw-root{color:rgb(0 43 54);--askw-accent:rgb(203 75 22);"))
        self.assertIn("html[data-askw-look] .askw-menu,html[data-askw-look] .askw-panel,html[data-askw-look] .askw-picker,"
                      "html[data-askw-look] .askw-chats-list{background:rgb(253 246 227/.97)}", css)
        self.assertIn(";font-family:" + FONT + "}", css)
        self.assertNotRegex(re.sub(r"font-family:[^}]*", "", css), r"url\(|var\(")
        self.assertEqual(reader_stylesheet(None), "")

    def test_the_panel_fills_its_buttons_with_the_vaults_button_never_its_link_colour(self):
        # The accent is only known to read on the vault's grounds, so white or ink on it can vanish (a dark vault's
        # cyan links left the Ask button 1:1). A filled button is the vault's own, at rest and under the pointer.
        for snapshots in (solarized(), dark()):
            look = palette(*snapshots)
            tokens, css = look["tokens"], reader_stylesheet(look)
            face, ink = f"background:rgb({tokens['--button-bg']});color:rgb({tokens['--button-ink']})", tokens["--button-ink"]
            with self.subTest(theme=look["mode"]):
                self.assertIn("html[data-askw-look] .askw-trigger,html[data-askw-look] .askw-ask-go,", css)
                self.assertIn(f"html[data-askw-look] .askw-history-actions button:first-child{{{face}}}", css)
                self.assertIn(f"html[data-askw-look] .askw-foot .askw-claude:hover{{background:rgb({tokens['--button-hover']});"
                              f"color:rgb({ink})}}", css)
                for fill in ("--button-bg", "--button-hover"):
                    self.assertGreaterEqual(contrast(rgb(ink), rgb(tokens[fill])), 4.5, fill)
                self.assertNotIn(f"background:rgb({tokens['--accent']})", css)

    def test_the_send_arrow_is_onyx_red_lifted_only_as_far_as_its_button_needs(self):
        # The arrow is an icon, so it marks at 3:1 on the button under it, at rest and under the pointer.
        for snapshots in (solarized(), dark()):
            look = palette(*snapshots)
            tokens, css = look["tokens"], reader_stylesheet(look)
            for state, fill in (("", "--button-bg"), (":hover", "--button-hover")):
                with self.subTest(theme=look["mode"], state=state or "rest"):
                    found = re.search(rf"html\[data-askw-look\] \.askw-follow-go{state}\{{color:rgb\(([\d ]+)\)\}}", css)
                    arrow = rgb(found.group(1))
                    self.assertGreaterEqual(contrast(arrow, rgb(tokens[fill])), 3.0)
                    self.assertGreater(arrow[0], 2 * max(arrow[1], arrow[2]))  # still red
        # On a light vault's ink button the logo's red already marks, so it is left as it is.
        self.assertIn("html[data-askw-look] .askw-follow-go{color:rgb(240 0 0)}", reader_stylesheet(palette(*solarized())))

    def test_the_glass_on_the_page_wears_the_vault_only_over_a_page_of_its_tone(self):
        css = reader_stylesheet(palette(*dark()))
        over_dark = 'html[data-askw-look][data-askw-page="dark"]'
        self.assertIn(f"{over_dark} .askw-pill b{{color:rgb(218 218 218)}}", css)
        self.assertIn(f"{over_dark} .askw-chats{{--askw-glass-accent:rgb(138 92 245)}}", css)
        self.assertNotRegex(css, r"html\[data-askw-look\] \.askw-(pill|chats)[\s,{]")  # never over a light page


class VaultLookApiTests(unittest.TestCase):
    def test_page_look_is_a_bounded_colour_calculation_with_the_usual_access_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AppConfig(default_folder=root, allowed_roots=(root,), port=8899, data_dir=root / "data")
            with TestClient(create_app(config), base_url="http://127.0.0.1:8899") as client:
                colors = {"background": "rgb(18, 20, 24)", "ink": "rgb(230, 232, 236)", "link": "rgb(23, 24, 25)"}
                self.assertEqual(client.post("/api/page-look", json=colors).status_code, 403)
                colors["token"] = config.token
                self.assertEqual(client.post("/api/page-look", json=colors, headers={"Origin": "https://evil.example"}).status_code, 403)
                result = client.post("/api/page-look", json=colors).json()
                self.assertEqual(result["mode"], "dark")
                self.assertEqual(result["base"], [18, 20, 24])
                self.assertIn("--accent:230 232 236", result["css"])
                self.assertNotIn("--ui-font", result["css"])
                # No link to measure: Onyx's own blue stays the accent wherever it reads (3:1), on cream and on black.
                for ground in ("rgb(253, 246, 227)", "rgb(0, 0, 0)"):
                    ink = "rgb(0, 0, 0)" if ground != "rgb(0, 0, 0)" else "rgb(255, 255, 255)"
                    unlinked = client.post("/api/page-look", json={"token": config.token, "background": ground, "ink": ink}).json()
                    self.assertIn("--accent:58 131 247", unlinked["css"])
                invalid = [
                    ("rgba(0, 0, 0, .5)", colors["ink"]),
                    ("red;}</style><script>alert(1)</script>", colors["ink"]),
                    (colors["background"], "rgb(20, 20, 20)"),
                    ({}, []), ("rgb(., 20, 30)", colors["ink"]),
                ]
                for background, ink in invalid:
                    bad = client.post("/api/page-look", json={**colors, "background": background, "ink": ink}).json()
                    self.assertEqual(bad["css"], "")
                self.assertTrue(client.get("/api/settings").json()["settings"]["html_follow_page"])
                client.post("/api/settings", json={"token": config.token, "settings": {"html_follow_page": False}})
                self.assertFalse(client.get("/api/vault-look").json()["page_enabled"])
                self.assertEqual(client.post("/api/page-look", json=colors).json()["css"], "")

    def test_the_shell_wears_the_vault_look_from_the_first_paint_while_the_switch_is_on(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "CX"
            (vault / "Inbox").mkdir(parents=True)
            (vault / "Inbox" / "note.md").write_text("# Note\n")
            config = AppConfig(default_folder=root, allowed_roots=(root,), port=8899, data_dir=root / "data")
            with TestClient(create_app(config), base_url="http://127.0.0.1:8899") as client:
                def settings(**patch):
                    self.assertEqual(client.post("/api/settings", json={"token": config.token, "settings": patch}).status_code, 200)

                settings(vault_root=str(vault))
                none = client.get("/api/vault-look").json()
                self.assertEqual((none["enabled"], none["available"], none["css"]), (True, False, ""))
                self.assertIn('<html data-theme="system"><head>', client.get("/").text)  # Onyx's own until the plugin measures

                markdown, sidebar = solarized()
                for kind, snapshot in (("markdown-theme", markdown), ("sidebar-theme", sidebar)):
                    body = {"token": config.token, "vault_root": str(vault), "snapshot": snapshot}
                    self.assertEqual(client.post(f"/api/{kind}", json=body).status_code, 200)
                look = client.get("/api/vault-look").json()
                self.assertEqual((look["available"], look["mode"], look["base"]), (True, "light", [253, 246, 227]))
                self.assertIn(":root.vault-look{--bg-primary:253 246 227;", look["css"])
                page = client.get("/").text
                self.assertIn('<html data-theme="system" class="vault-look">', page)
                self.assertIn(f"<style id=vault-look>{look['css']}</style>", page)
                self.assertIn('vault:{"mode": "light", "base": [253, 246, 227]}', page)  # the glass and window on the cream
                self.assertIn("id=vault-look-toggle", page)
                self.assertNotIn("name=sidebar_follow_obsidian", page)  # one switch, not two
                self.assertIn("html[data-askw-look] .askw-panel", look["reader_css"])  # the answer panel too
                # The reader: a text page wears the vault's reading styles, as a note does; so does a shared selection.
                text = vault / "Inbox" / "plain.txt"
                text.write_text("Plain words.\n")
                for response in (client.get("/view", params={"src": str(text)}), client.get("/quick", params={"text": "A passage"})):
                    self.assertIn('<style id="askw-markdown-theme">:is(body[data-askw-document-kind="markdown"]', response.text)

                # The switch sets both settings; with both off the app is back in its own look, and says so in its revision.
                settings(markdown_follow_obsidian=False, sidebar_follow_obsidian=False)
                off = client.get("/api/vault-look").json()
                self.assertEqual((off["enabled"], off["available"], off["css"], off["mode"]), (False, True, "", None))
                self.assertEqual(off["reader_css"], "")
                self.assertNotEqual(off["revision"], look["revision"])
                self.assertNotIn('class="vault-look"', client.get("/").text)


if __name__ == "__main__":
    unittest.main()
