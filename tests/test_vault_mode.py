"""The vault's two colour modes: stored side by side, picked by Settings' Color theme (vault_mode), published both."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from onyx import vault_mode
from onyx.app import create_app
from onyx.config import AppConfig
from onyx.storage import Storage

LIGHT_GROUND, LIGHT_INK = "rgb(253, 246, 227)", "rgb(0, 43, 54)"
DARK_GROUND, DARK_INK = "rgb(0, 43, 54)", "rgb(238, 232, 213)"


def markdown(mode: str) -> dict:
    ground, ink = (LIGHT_GROUND, LIGHT_INK) if mode == "light" else (DARK_GROUND, DARK_INK)
    return {"mode": mode, "styles": {"content": {"color": ink, "background-color": ground},
                                     "a": {"color": "rgb(38, 139, 210)"}}}


def sidebar(mode: str) -> dict:
    ground, ink = ("rgb(238, 232, 213)", LIGHT_INK) if mode == "light" else ("rgb(7, 54, 66)", DARK_INK)
    return {"mode": mode, "styles": {"pane": {"background-color": ground, "color": ink},
                                     "file": {"color": ink}}, "folders": []}


class StorageTests(unittest.TestCase):
    def test_both_modes_are_kept_and_a_sync_without_the_other_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Storage(root / "data")
            self.addCleanup(store.close)
            store.save_markdown_theme(root, markdown("light"), markdown("dark"))
            self.assertEqual(store.markdown_theme(root), markdown("light"), "Obsidian's own mode by default")
            self.assertEqual(store.markdown_theme(root, "light"), markdown("light"))
            self.assertEqual(store.markdown_theme(root, "dark"), markdown("dark"))
            # A heartbeat (or an older plugin) sends only the mode Obsidian shows.
            store.save_markdown_theme(root, markdown("light"))
            self.assertEqual(store.markdown_theme(root, "dark"), markdown("dark"))
            # Obsidian switched to dark: what was the other mode is now its own, and the old one is the other.
            store.save_markdown_theme(root, markdown("dark"), markdown("light"))
            self.assertEqual(store.markdown_theme(root)["mode"], "dark")
            self.assertEqual(store.markdown_theme(root, "light"), markdown("light"))
            # A stored other that is now the same mode as Obsidian's is never handed out as the other mode.
            store.save_sidebar_theme(root, sidebar("light"), sidebar("dark"))
            store.save_sidebar_theme(root, sidebar("dark"))
            self.assertEqual(store.sidebar_theme(root, "dark"), sidebar("dark"))
            self.assertIsNone(store.sidebar_theme(root, "light"))
            reopened = Storage(root / "data")
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.markdown_theme(root, "light"), markdown("light"), "survives a reopen")

    def test_a_database_from_before_both_modes_gains_the_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            Storage(data).close()  # create the current schema, then take it back to the old theme tables
            db = sqlite3.connect(data / "onyx.db")
            for table in ("markdown_themes", "sidebar_themes"):
                db.execute(f"DROP TABLE {table}")
                db.execute(f"CREATE TABLE {table} (vault_root TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, "
                           "updated_at REAL NOT NULL)")
            db.execute("INSERT INTO markdown_themes VALUES(?, ?, 0)", (str(Path(temp).resolve()), '{"mode": "light"}'))
            db.commit()
            db.close()
            store = Storage(data)
            self.addCleanup(store.close)
            self.assertEqual(store.markdown_theme(Path(temp)), {"mode": "light"})
            self.assertIsNone(store.markdown_theme(Path(temp), "dark"))
            store.save_markdown_theme(Path(temp), markdown("light"), markdown("dark"))
            self.assertEqual(store.markdown_theme(Path(temp), "dark"), markdown("dark"))


class ChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Storage(self.root / "data")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_each_choice_picks_one_mode_for_both_snapshots(self) -> None:
        self.store.save_markdown_theme(self.root, markdown("light"), markdown("dark"))
        self.store.save_sidebar_theme(self.root, sidebar("light"), sidebar("dark"))
        for choice, mode in (("obsidian", "light"), (None, "light"), ("bogus", "light"), ("light", "light"), ("dark", "dark")):
            with self.subTest(choice=choice):
                md, sb = vault_mode.snapshots(self.store, self.root, choice)
                self.assertEqual((md["mode"], sb["mode"]), (mode, mode))
        for system in ("light", "dark"):
            with self.subTest(system=system), mock.patch.object(vault_mode, "system_mode", return_value=system):
                md, sb = vault_mode.snapshots(self.store, self.root, "system")
                self.assertEqual((md["mode"], sb["mode"]), (system, system))
        self.assertEqual(vault_mode.available(self.store, self.root), ["light", "dark"])

    def test_a_mode_not_measured_yet_falls_back_to_obsidians(self) -> None:
        self.store.save_markdown_theme(self.root, markdown("light"))
        self.store.save_sidebar_theme(self.root, sidebar("light"))
        md, sb = vault_mode.snapshots(self.store, self.root, "dark")
        self.assertEqual((md["mode"], sb["mode"]), ("light", "light"))
        self.assertEqual(vault_mode.available(self.store, self.root), ["light"])

    def test_a_missing_sidebar_mode_is_left_out_rather_than_mixed(self) -> None:
        self.store.save_markdown_theme(self.root, markdown("light"), markdown("dark"))
        self.store.save_sidebar_theme(self.root, sidebar("light"))
        md, sb = vault_mode.snapshots(self.store, self.root, "dark")
        self.assertEqual(md["mode"], "dark")
        self.assertIsNone(sb, "a light explorer under a dark reading view would pair one mode's ink with the other's ground")

    def test_system_mode_is_read_at_most_once_a_second(self) -> None:
        with mock.patch.object(vault_mode, "_cache", None), \
             mock.patch.object(vault_mode, "_read_interface_style", side_effect=["Dark", None]) as read, \
             mock.patch.object(vault_mode.time, "monotonic", side_effect=[100.0, 100.5, 101.6]):
            self.assertEqual(vault_mode.system_mode(), "dark")
            self.assertEqual(vault_mode.system_mode(), "dark")
            self.assertEqual(vault_mode.system_mode(), "light", "unset (or unreadable) is light")
            self.assertEqual(read.call_count, 2)


class ApiTests(unittest.TestCase):
    def test_color_theme_picks_the_vaults_mode_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "note.md").write_text("# Note")
            config = AppConfig(default_folder=root, allowed_roots=(root,), port=8899, data_dir=root / "data")
            with TestClient(create_app(config), base_url="http://127.0.0.1:8899") as client:
                def settings(**patch):
                    return client.post("/api/settings", json={"token": config.token, "settings": patch})

                self.assertEqual(settings(vault_root=str(root)).status_code, 200)
                self.assertEqual(settings(vault_mode="sepia").status_code, 400)
                body = {"token": config.token, "vault_root": str(root)}
                self.assertEqual(client.post("/api/markdown-theme", json={
                    **body, "snapshot": markdown("light"), "other": markdown("light")}).status_code, 400,
                    "the other mode has to be the other mode")
                self.assertEqual(client.post("/api/markdown-theme", json={
                    **body, "snapshot": markdown("light"), "other": {"mode": "dark", "styles": {"body": {}}}}).status_code,
                    400, "and is validated as strictly")
                self.assertEqual(client.post("/api/markdown-theme", json={**body, "snapshot": markdown("light")}).status_code, 200)
                self.assertEqual(client.post("/api/sidebar-theme", json={**body, "snapshot": sidebar("light")}).status_code, 200)

                look = client.get("/api/vault-look").json()
                self.assertEqual((look["mode"], look["choice"], look["wanted"], look["modes"]), ("light", "obsidian", None, ["light"]))
                # Dark asked for before the plugin has measured it: the vault look stays, in the mode there is.
                settings(vault_mode="dark")
                look = client.get("/api/vault-look").json()
                self.assertEqual((look["mode"], look["wanted"], look["modes"]), ("light", "dark", ["light"]))

                self.assertEqual(client.post("/api/markdown-theme", json={
                    **body, "snapshot": markdown("light"), "other": markdown("dark")}).status_code, 200)
                self.assertEqual(client.post("/api/sidebar-theme", json={
                    **body, "snapshot": sidebar("light"), "other": sidebar("dark")}).status_code, 200)
                look = client.get("/api/vault-look").json()
                self.assertEqual((look["mode"], look["modes"]), ("dark", ["light", "dark"]))
                self.assertIn(DARK_GROUND, client.get("/api/markdown-theme").json()["css"])
                self.assertIn("rgb(7, 54, 66)", client.get("/api/sidebar-theme").json()["css"])
                page = client.get("/view", params={"src": str(root / "note.md")}).text
                self.assertIn('data-askw-look="dark"', page, "a page's first paint is already the chosen mode")
                self.assertIn(DARK_GROUND, page)

                settings(vault_mode="light")
                self.assertEqual(client.get("/api/vault-look").json()["mode"], "light")
                self.assertIn(LIGHT_GROUND, client.get("/api/markdown-theme").json()["css"])
                for system in ("dark", "light"):
                    with mock.patch.object(vault_mode, "system_mode", return_value=system):
                        settings(vault_mode="system")
                        self.assertEqual(client.get("/api/vault-look").json()["mode"], system)
                settings(vault_mode="obsidian")
                self.assertEqual(client.get("/api/vault-look").json()["mode"], "light")
