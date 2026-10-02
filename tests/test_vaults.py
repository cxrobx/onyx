"""Other notes vaults beside the primary (Settings ▸ Vaults ▸ Other vaults): each its own tree, links and context, and
searched with ⌘P when the scope says every vault. The primary keeps everything it had, unchanged."""

from __future__ import annotations

import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from onyx import search
from onyx.app import create_app
from onyx.config import AppConfig
from onyx.vault import VaultCache

try:  # discovered from tests/ (CI), or run as tests.test_vaults from the checkout
    from test_search import API_NEUTRAL, NEAR_A, NEAR_B, OFF, make_index, stand_in
except ImportError:  # pragma: no cover
    from tests.test_search import API_NEUTRAL, NEAR_A, NEAR_B, OFF, make_index, stand_in

LABEL_NEAR = [0, 1, 0, 0.2, 0]  # the other vault's subject, nobody's in the primary


class OtherVaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.base = base
        self.notes, self.label, self.artifacts = base / "CX", base / "Dark Label", base / "Artifacts"
        for folder in (self.notes / "Projects", self.label / "Sessions", self.artifacts):
            folder.mkdir(parents=True)
        # One name in both vaults: a wikilink must stay in the vault it is written in.
        (self.notes / "Pricing.md").write_text("# Pricing\n\nThe CX rate card.\n", encoding="utf-8")
        (self.notes / "Projects" / "Deploy.md").write_text("# Deploy\n\n## NAS\n\nThe nas-tunnel.\n", encoding="utf-8")
        (self.label / "Pricing.md").write_text("# Pricing\n\nWhat a producer charges per beat.\n", encoding="utf-8")
        self.session = self.label / "Sessions" / "Outreach.md"
        self.session.write_text("# Outreach\n\n## Cold DMs\n\nSee [[Pricing]] before the first message.\n", encoding="utf-8")
        primary_db, label_db = base / "index.db", base / "darklabel.db"
        make_index(primary_db, [
            ("Projects/Deploy.md", "Deploy > NAS", "The nas-tunnel carries every service out.", NEAR_A),
            ("Pricing.md", "Pricing", "The CX rate card for advisory work.", NEAR_B),
            ("Bulk.md", "Bulk", "Filler about other matters.", OFF),
        ])
        make_index(label_db, [
            ("Sessions/Outreach.md", "Outreach > Cold DMs", "Cold DMs to artists, before the first message.", LABEL_NEAR),
            ("Pricing.md", "Pricing", "What a producer charges per beat; the rate card.", [0, 1, 0, 0.3, 0]),
            ("Sessions/Bulk.md", "Bulk", "More filler elsewhere.", OFF),
        ])
        # vault-mcp's configs: the primary's (no db: it writes the default index) and the other vault's own.
        self.configs = base / "vault-mcp"
        self.configs.mkdir()
        (self.configs / "config.toml").write_text(f'vault = "{self.notes}"\n', encoding="utf-8")
        (self.configs / "darklabel.toml").write_text(
            f'vault = "{self.label}"\ndb = "{label_db}"\nmounts = []\n', encoding="utf-8"
        )
        env = mock.patch.dict("os.environ", {"ONYX_VAULT_MCP_CONFIGS": str(self.configs)})
        env.start()
        self.addCleanup(env.stop)
        self.config = AppConfig(default_folder=base, allowed_roots=(base,), port=8899, data_dir=base / "data")
        self.app = create_app(self.config)
        self.settings({"vault_root": str(self.notes), "html_vault_root": str(self.artifacts)})
        embed = stand_in({"rate card": [0, 1, 0, 0.25, 0], "nas": NEAR_A}, API_NEUTRAL)
        self.app.state.passages = search.PassageIndex(primary_db, embed=embed)
        self.app.state.extra_passages[label_db] = search.PassageIndex(label_db, embed=embed)
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def settings(self, patch: dict) -> dict:
        return self.app.state.storage.update_settings(patch, model_default=self.config.model)

    def add_label(self) -> None:
        response = self.client.post(
            "/api/settings", json={"token": self.config.token, "settings": {"extra_vault_roots": [str(self.label)]}}
        )
        self.assertEqual(response.status_code, 200, response.text)

    # MARK: settings

    def test_other_vaults_must_be_folders_of_their_own(self) -> None:
        self.assertEqual(self.settings({})["extra_vault_roots"], [])
        self.assertEqual(self.settings({})["search_scope"], "primary")
        for bad in (
            [str(self.base / "missing")],  # not there
            [str(self.notes)],  # the primary itself
            [str(self.notes / "Projects")],  # inside the primary
            [str(self.base)],  # holding the primary
            [str(self.label), str(self.label)],  # twice
            "not a list",
        ):
            with self.assertRaises(ValueError, msg=bad):
                self.settings({"extra_vault_roots": bad})
        with self.assertRaises(ValueError):
            self.settings({"search_scope": "everything"})
        saved = self.settings({"extra_vault_roots": [str(self.label) + "/", ""], "search_scope": "all"})
        self.assertEqual((saved["extra_vault_roots"], saved["search_scope"]), ([str(self.label)], "all"))

    def test_the_vaults_are_listed_in_order_and_each_can_be_read_as_context(self) -> None:
        self.add_label()
        vaults = self.client.get("/api/vaults").json()["vaults"]
        self.assertEqual(
            [(v["key"], v["kind"], v["label"]) for v in vaults],
            [("notes", "notes", "Notes"), ("v-dark-label", "notes", "Dark Label"), ("html", "html", "Artifacts")],
        )
        roots = {r["path"] for r in self.client.get("/api/settings").json()["roots"]}
        self.assertIn(str(self.label.resolve()), roots)  # answers may cite the other vault's notes

    # MARK: trees and routes

    def test_each_vault_has_its_own_tree_and_index(self) -> None:
        self.add_label()
        label = self.client.get("/api/vault/tree", params={"vault": "v-dark-label"}).json()
        self.assertEqual((label["vault"], label["root"], label["files"]), ("v-dark-label", str(self.label), 2))
        notes = self.client.get("/api/vault/tree", params={"vault": "notes"}).json()
        self.assertEqual((notes["vault"], notes["root"]), ("notes", str(self.notes)))
        # Anything not a vault's key is the primary, as every route read it before there were others.
        self.assertEqual(self.client.get("/api/vault/tree", params={"vault": "nonsense"}).json()["root"], str(self.notes))
        found = self.client.get("/api/vault/search", params={"vault": "v-dark-label", "q": "outreach"}).json()
        self.assertEqual([i["path"] for i in found["items"]], [str(self.session)])
        cache = VaultCache(ttl=60)
        self.assertIsNot(cache.get(self.notes), cache.get(self.label))
        self.assertIs(cache.get(self.label), cache.get(self.label))

    def test_a_row_is_revealed_only_from_inside_its_own_vault(self) -> None:
        self.add_label()
        inside = self.client.get("/api/vault/entry", params={"vault": "v-dark-label", "path": str(self.session)})
        self.assertEqual(inside.json()["path"], str(self.session))
        outside = self.client.get(
            "/api/vault/entry", params={"vault": "v-dark-label", "path": str(self.notes / "Pricing.md")}
        )
        self.assertEqual(outside.status_code, 400)
        refused = self.client.post("/api/vault/reveal", json={
            "token": self.config.token, "vault": "v-dark-label", "path": str(self.notes / "Pricing.md"),
        })
        self.assertEqual(refused.status_code, 400)

    def test_a_note_reads_with_its_own_vaults_links_and_context(self) -> None:
        self.add_label()
        page = self.client.get("/view", params={"src": str(self.session), "folder": str(self.label)})
        self.assertEqual(page.status_code, 200)
        # [[Pricing]] is Dark Label's Pricing, never the primary's note of the same name.
        self.assertIn(urllib.parse.quote(str(self.label / "Pricing.md")), page.text)
        self.assertNotIn(urllib.parse.quote(str(self.notes / "Pricing.md")), page.text)
        cap = re.search(r'name="askw-doc-token" content="([^"]+)"', page.text).group(1)
        self.assertEqual(self.app.state.asset_caps[cap]["editing"]["notes"], str(self.label))
        # Opened from outside (Finder, Library), it is placed in the vault it lives in.
        located = self.client.get("/api/vault/locate", params={"src": str(self.session)}).json()
        self.assertEqual((located["vault"], located["path"]), ("v-dark-label", str(self.session)))
        shell = self.client.get("/", params={"src": str(self.session)})
        self.assertIn(urllib.parse.quote(str(self.label), safe="/"), shell.text)  # its vault is its folder

    def test_without_other_vaults_the_shell_and_routes_are_as_before(self) -> None:
        shell = self.client.get("/vault").text
        self.assertIn("setExtra([])", shell)
        self.assertEqual([v["key"] for v in self.client.get("/api/vaults").json()["vaults"]], ["notes", "html"])
        result = self.client.get("/api/search", params={"q": "rate card"}).json()
        self.assertEqual(result["scope"], "primary")
        self.assertNotIn("missing", result)

    # MARK: search

    def test_search_covers_the_primary_unless_asked_for_every_vault(self) -> None:
        self.add_label()
        primary = self.client.get("/api/search", params={"q": "rate card"}).json()
        self.assertEqual(primary["scope"], "primary")
        self.assertEqual({i["vault"] for i in primary["items"]}, {"notes"})
        every = self.client.get("/api/search", params={"q": "rate card", "scope": "all"}).json()
        self.assertEqual(every["scope"], "all")
        self.assertEqual({i["vault"] for i in every["items"]}, {"notes", "v-dark-label"})
        label_rows = [i for i in every["items"] if i["vault"] == "v-dark-label"]
        self.assertIn(str(self.label / "Pricing.md"), [i["path"] for i in label_rows])
        # The setting is the default the palette starts from.
        self.settings({"search_scope": "all"})
        self.assertEqual(self.client.get("/api/search", params={"q": "rate card"}).json()["scope"], "all")

    def test_a_vault_with_no_index_of_its_own_is_named_rather_than_missed(self) -> None:
        (self.configs / "darklabel.toml").unlink()
        self.add_label()
        result = self.client.get("/api/search", params={"q": "rate card", "scope": "all"}).json()
        self.assertEqual({i["vault"] for i in result["items"]}, {"notes"})
        self.assertEqual(result["missing"], ["Dark Label: not in a vault-mcp index"])

    def test_one_index_searched_alone_answers_as_it_always_did(self) -> None:
        index = self.app.state.passages
        place = lambda path: {"vault": "notes", "path": path, "title": path, "folder": ""}  # noqa: E731
        for query in ("nas", "rate card", "nas tunnel", "x"):
            self.assertEqual(index.search(query, place=place), search.search_many([(index, place, "")], query), query)

    def test_related_stays_inside_the_vault_the_page_is_in(self) -> None:
        self.add_label()
        result = self.client.get("/api/related", params={"vault": "v-dark-label", "path": str(self.session)}).json()
        self.assertEqual(result["note"], "Sessions/Outreach.md")
        self.assertTrue(all(i["vault"] == "v-dark-label" for i in result["items"]), result["items"])
        primary = self.client.get("/api/related", params={"vault": "notes", "path": str(self.notes / "Projects" / "Deploy.md")}).json()
        self.assertEqual(primary["note"], "Projects/Deploy.md")
        self.assertTrue(all(i["vault"] == "notes" for i in primary["items"]), primary["items"])

    def test_vault_indexes_read_only_configs_that_name_their_own_index(self) -> None:
        (self.configs / "broken.toml").write_text("vault = [", encoding="utf-8")
        self.assertEqual(search.vault_indexes(self.configs), {str(self.label): self.base / "darklabel.db"})
        self.assertEqual(search.vault_indexes(self.base / "nowhere"), {})

    # MARK: look

    def test_a_note_wears_its_own_vaults_reading_styles_once_measured(self) -> None:
        self.add_label()
        storage = self.app.state.storage
        storage.save_markdown_theme(self.notes, {"mode": "light", "styles": {"content": {"color": "rgb(1, 2, 3)"}}}, None)
        theme = lambda **params: self.client.get("/api/markdown-theme", params=params).json()["css"]  # noqa: E731
        primary_css = theme()
        self.assertIn("rgb(1, 2, 3)", primary_css)
        # Never measured, the other vault's note wears the primary's styles rather than none.
        self.assertEqual(theme(src=str(self.session)), primary_css)
        storage.save_markdown_theme(self.label, {"mode": "dark", "styles": {"content": {"color": "rgb(7, 8, 9)"}}}, None)
        self.assertIn("rgb(7, 8, 9)", theme(src=str(self.session)))
        page = self.client.get("/view", params={"src": str(self.session), "folder": str(self.label)}).text
        self.assertIn("rgb(7, 8, 9)", page)  # the first paint already wears it, so the poll changes nothing
        # A primary note, and a page that names no vault, keep the primary's.
        self.assertEqual(theme(src=str(self.notes / "Pricing.md")), primary_css)
        self.assertEqual(theme(src="service://selection/x"), primary_css)

if __name__ == "__main__":
    unittest.main()
