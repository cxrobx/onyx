"""Artifacts links follow a page moved or renamed outside Onyx (link_repair.py).

The failure class: Artifacts is a folder of symlinks, a symlink holds a path, and a page moved in Finder left its
entry pointing at nothing — every one of them showing as missing at once. Each listing now records every working
link's target by its identity on this Mac and, once a link breaks, re-aims it at where that identity now is, when
that is certain. When it is not, the row offers a guess and links only what is chosen.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from onyx import link_repair, vault
from onyx.app import create_app
from onyx.config import AppConfig
from onyx.storage import Storage

ON_MAC = unittest.skipUnless(sys.platform == "darwin", "file identities are looked up through macOS's /.vol")


class _Linked(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / "Artifacts"
        (self.vault / "Muzik").mkdir(parents=True)
        self.docs = self.base / "Documents" / "Music"
        self.docs.mkdir(parents=True)
        self.page = self.docs / "The Room.html"
        self.page.write_text("<title>The Room</title>", encoding="utf-8")
        self.guide = self.base / "learnings" / "guide"
        self.guide.mkdir(parents=True)
        (self.guide / "index.html").write_text("<title>Guide</title>", encoding="utf-8")
        self.page_link = self.vault / "Muzik" / "The Room.html"
        self.page_link.symlink_to(self.page)
        self.folder_link = self.vault / "Guide"
        self.folder_link.symlink_to(self.guide, target_is_directory=True)
        self.storage = Storage(self.base / "data")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def sweep(self) -> list[tuple[str, str]]:
        return link_repair.sweep(self.vault, self.storage)


class SweepTests(_Linked):
    @ON_MAC
    def test_a_page_and_a_folder_moved_in_finder_are_relinked_where_they_went(self) -> None:
        self.assertEqual(self.sweep(), [])  # first listing: records, repairs nothing
        elsewhere = self.base / "Archive" / "2026"
        elsewhere.mkdir(parents=True)
        moved_page = elsewhere / "Renamed Room.html"
        os.rename(self.page, moved_page)  # moved AND renamed, as Finder can
        moved_guide = self.base / "Archive" / "guide-v2"
        os.rename(self.guide, moved_guide)
        self.assertFalse(self.page_link.exists())

        repaired = dict(self.sweep())
        self.assertEqual(repaired, {str(self.page_link): str(moved_page), str(self.folder_link): str(moved_guide)})
        # The entries kept their names and places; only where they point changed, and the files were not touched.
        self.assertTrue(self.page_link.is_symlink())
        self.assertEqual(os.readlink(self.page_link), str(moved_page))
        self.assertEqual((self.folder_link / "index.html").read_text(encoding="utf-8"), "<title>Guide</title>")
        self.assertEqual(moved_page.read_text(encoding="utf-8"), "<title>The Room</title>")
        self.assertEqual(self.sweep(), [])  # and the new place is what is remembered
        self.assertEqual(self.storage.link_targets(str(self.vault))[str(self.page_link)][0], str(moved_page))

    @ON_MAC
    def test_a_page_moved_to_the_trash_is_left_missing(self) -> None:
        # A deleted file keeps its identity in the Trash; following it there would bring a deleted page back.
        self.sweep()
        trash = self.base / ".Trash"
        trash.mkdir()
        os.rename(self.page, trash / self.page.name)
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.page_link.exists())
        self.assertEqual(os.readlink(self.page_link), str(self.page))

    @ON_MAC
    def test_a_page_moved_into_artifacts_itself_is_not_linked_to(self) -> None:
        self.sweep()
        os.rename(self.page, self.vault / "Muzik" / "moved-in.html")
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.page_link.exists())

    def test_a_link_that_broke_before_anything_was_recorded_is_left_for_the_guess(self) -> None:
        os.rename(self.page, self.docs / "elsewhere.html")
        self.assertEqual(self.sweep(), [])
        self.assertNotIn(str(self.page_link), self.storage.link_targets(str(self.vault)))

    def test_only_what_changed_is_written_and_removed_links_are_forgotten(self) -> None:
        self.sweep()
        recorded = self.storage.link_targets(str(self.vault))
        self.assertEqual(set(recorded), {str(self.page_link), str(self.folder_link)})
        writes: list = []
        real = self.storage.remember_link_targets
        self.storage.remember_link_targets = lambda rows, forget=(): (writes.append((rows, list(forget))), real(rows, forget))
        self.sweep()
        self.assertEqual(writes, [({}, [])])  # a listing with nothing new writes nothing
        self.page_link.unlink()
        self.sweep()
        self.assertEqual(writes[-1], ({}, [str(self.page_link)]))
        self.assertEqual(set(self.storage.link_targets(str(self.vault))), {str(self.folder_link)})

    def test_links_inside_a_linked_folder_are_not_the_vaults_to_repair(self) -> None:
        (self.guide / "inner.html").symlink_to(self.page)
        self.assertNotIn(self.folder_link / "inner.html", link_repair.owned_links(self.vault))
        self.assertIn(self.page_link, link_repair.owned_links(self.vault))

    def test_what_can_stand_in_for_a_lost_target(self) -> None:
        self.assertTrue(link_repair.fits(self.vault, self.page, False))
        self.assertFalse(link_repair.fits(self.vault, self.page, True))  # a page for a folder
        self.assertFalse(link_repair.fits(self.vault, self.guide, False))
        notes = self.docs / "notes.md"
        notes.write_text("x", encoding="utf-8")
        self.assertFalse(link_repair.fits(self.vault, notes, False))  # not a page
        self.assertFalse(link_repair.fits(self.vault, self.base / "gone.html", False))
        self.assertFalse(link_repair.fits(self.vault, self.base, True))  # holds Artifacts: a loop


class GuessTests(_Linked):
    def test_a_page_moved_nearby_is_found_by_name(self) -> None:
        os.rename(self.page, self.base / "Documents" / self.page.name)  # up a folder
        found = link_repair.candidates(self.vault, self.page_link, finder=lambda _n: [])
        self.assertEqual(found["candidates"], [str(self.base / "Documents" / self.page.name)])
        self.assertEqual(found["was"], str(self.page))
        self.assertFalse(found["is_dir"])
        self.assertEqual(found["near"], str(self.docs))

    def test_spotlight_widens_the_guess_and_wrong_kinds_drop_out(self) -> None:
        far = self.base / "far" / "away"
        far.mkdir(parents=True)
        os.rename(self.page, far / self.page.name)
        decoy = self.base / "decoy" / self.page.name
        decoy.mkdir(parents=True)  # a folder by the page's name
        asked: list[str] = []

        def finder(name: str) -> list[str]:
            asked.append(name)
            return [str(far / self.page.name), str(decoy), str(far / self.page.name)]

        found = link_repair.candidates(self.vault, self.page_link, finder=finder)
        self.assertEqual(asked, [self.page.name])
        self.assertEqual(found["candidates"], [str(far / self.page.name)])

    def test_a_guides_index_page_is_matched_by_its_folder_too(self) -> None:
        link = self.vault / "Muzik" / "guide.html"
        link.symlink_to(self.guide / "index.html")
        moved = self.base / "Documents" / "guide"
        os.rename(self.guide, moved)
        other = self.base / "Documents" / "other"
        other.mkdir()
        (other / "index.html").write_text("<title>Other</title>", encoding="utf-8")
        found = link_repair.candidates(self.vault, link, finder=lambda _n: [str(other / "index.html")])
        self.assertEqual(found["candidates"], [str(moved / "index.html")])

    def test_a_working_link_or_a_folder_of_artifacts_own_has_nothing_to_guess(self) -> None:
        self.assertEqual(link_repair.candidates(self.vault, self.page_link, finder=lambda _n: [])["candidates"], [])
        with self.assertRaises(ValueError):
            link_repair.candidates(self.vault, self.vault / "Muzik", finder=lambda _n: [])


class RelinkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.context = self.base / "context"
        self.context.mkdir()
        self.vault = self.base / "Artifacts"
        (self.vault / "Mine").mkdir(parents=True)
        self.docs = self.base / "docs"
        self.docs.mkdir()
        self.page = self.docs / "report.html"
        self.page.write_text("<title>Quarterly report</title>", encoding="utf-8")
        self.link = self.vault / "Mine" / "report.html"
        self.link.symlink_to(self.page)
        self.config = AppConfig(
            default_folder=self.context, allowed_roots=(self.context,), port=8899, data_dir=self.base / "data"
        )
        self.app = create_app(self.config)
        self.app.state.storage.update_settings({"html_vault_root": str(self.vault)}, model_default="sonnet")
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def tree_row(self) -> dict:
        self.app.state.vault.invalidate()
        tree = self.client.get("/api/vault/tree", params={"vault": "html"}).json()["tree"]
        mine = next(c for c in tree["children"] if c["name"] == "Mine")
        return mine["children"][0]

    @ON_MAC
    def test_the_listing_repairs_a_page_moved_in_finder(self) -> None:
        self.assertNotIn("missing", self.tree_row())  # recorded
        moved = self.base / "archive" / "q3.html"
        moved.parent.mkdir()
        os.rename(self.page, moved)
        row = self.tree_row()
        self.assertNotIn("missing", row)
        self.assertEqual(row["title"], "Quarterly report")
        self.assertEqual(os.readlink(self.link), str(moved))

    def test_a_missing_page_is_offered_where_it_went_and_relinked_when_chosen(self) -> None:
        moved = self.base / "report.html"
        os.rename(self.page, moved)  # never recorded: the guess's case
        self.assertTrue(self.tree_row().get("missing"))
        found = self.client.get("/api/vault/html/relink", params={"path": str(self.link)}).json()
        self.assertTrue(found["ok"])
        self.assertIn(str(moved), found["candidates"])

        token = {"token": self.config.token}
        inside = self.client.post(
            "/api/vault/html/relink", json={**token, "path": str(self.link), "target": str(self.vault / "Mine")}
        )
        self.assertEqual(inside.status_code, 400)
        self.assertIn("already inside Artifacts", inside.json()["error"])
        self.assertEqual(
            self.client.post("/api/vault/html/relink", json={"path": str(self.link), "target": str(moved)}).status_code,
            403,
        )
        done = self.client.post("/api/vault/html/relink", json={**token, "path": str(self.link), "target": str(moved)})
        self.assertEqual(done.json(), {"ok": True, "path": str(self.link), "target": str(moved)})
        self.assertEqual(self.tree_row()["title"], "Quarterly report")
        self.assertEqual(self.app.state.storage.link_targets(str(self.vault))[str(self.link)][0], str(moved))

        folder = self.client.post(
            "/api/vault/html/relink", json={**token, "path": str(self.vault / "Mine"), "target": str(moved)}
        )
        self.assertEqual(folder.status_code, 400)
        self.assertIn("not a link", folder.json()["error"])
        notes = self.client.get("/api/vault/notes/relink", params={"path": str(self.link)})
        self.assertEqual(notes.status_code, 400)

    def test_a_failing_repair_never_breaks_the_listing(self) -> None:
        def broken(*_a, **_k):
            raise RuntimeError("disk on fire")

        original = link_repair.sweep
        link_repair.sweep = broken
        try:
            with self.assertLogs("onyx.app", level="ERROR"):
                self.assertEqual(self.tree_row()["title"], "Quarterly report")
        finally:
            link_repair.sweep = original


if __name__ == "__main__":
    unittest.main()
