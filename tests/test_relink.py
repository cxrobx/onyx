from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onyx import relink, vault as vault_mod
from onyx.vault import VaultIndex


def _move(root: Path, path: Path, dest: str) -> tuple[Path, dict]:
    """What the notes move route does: plan the links, move, then write them."""
    index = VaultIndex.build(root)
    entry, moved = vault_mod.plan_move(root, path, dest, "Notes")
    planned = relink.plan(index, relink.file_moves(index, entry, moved))
    vault_mod.move_entry(root, path, dest, "Notes")
    return moved, relink.apply(root, planned)


def _rename(root: Path, path: Path, name: str) -> tuple[Path, dict]:
    index = VaultIndex.build(root)
    entry, renamed = vault_mod.plan_rename(root, path, name, "Notes")
    planned = relink.plan(index, relink.file_moves(index, entry, renamed))
    vault_mod.rename_entry(root, path, name, "Notes")
    return renamed, relink.apply(root, planned)


class RelinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self._tmp.name)) / "vault"
        for folder in ("Projects/Alpha", "Areas", "Other/Attachments", "Archive"):
            (self.root / folder).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.write_text(text, encoding="utf-8")
        return path

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def test_moving_a_note_keeps_every_kind_of_link_to_it_and_from_it_pointing(self) -> None:
        self.write("Projects/Alpha/Plan.md", "# Plan\n\n[up](../../Areas/Goals.md) ![pic](../../Other/Attachments/a%20b.png) "
                   "[[Goals]] [same](Sibling.md)\n")
        self.write("Projects/Alpha/Sibling.md", "[plan](Plan.md) and [[Projects/Alpha/Plan#Steps|the plan]] and [[Plan]]\n")
        self.write("Areas/Goals.md", "See [the plan](../Projects/Alpha/Plan.md#steps \"title\") and <x> [v](Projects/Alpha/Plan.md)\n"
                   "`[[Projects/Alpha/Plan]]` and\n\n```\n[x](../Projects/Alpha/Plan.md)\n```\n")
        (self.root / "Other/Attachments/a b.png").write_bytes(b"png")

        moved, result = _move(self.root, self.root / "Projects/Alpha/Plan.md", "Archive")

        self.assertEqual(moved, self.root / "Archive/Plan.md")
        plan = self.read("Archive/Plan.md")
        # Its own relative links, recomputed from its new folder, in the same encoding; a name link needs nothing.
        self.assertIn("[up](../Areas/Goals.md) ![pic](../Other/Attachments/a%20b.png) [[Goals]] [same](../Projects/Alpha/Sibling.md)", plan)
        sibling = self.read("Projects/Alpha/Sibling.md")
        self.assertIn("[plan](../../Archive/Plan.md)", sibling)
        # A path wikilink goes to the shortest form that still resolves, keeping its heading and alias.
        self.assertIn("[[Plan#Steps|the plan]] and [[Plan]]", sibling)
        goals = self.read("Areas/Goals.md")
        self.assertIn('[the plan](../Archive/Plan.md#steps "title")', goals)
        self.assertIn("[v](Archive/Plan.md)", goals)  # written from the vault's root, kept so
        # Code is code.
        self.assertIn("`[[Projects/Alpha/Plan]]`", goals)
        self.assertIn("[x](../Projects/Alpha/Plan.md)\n```", goals)
        self.assertEqual(sorted(result["updated"]), ["Archive/Plan.md", "Areas/Goals.md", "Projects/Alpha/Sibling.md"])
        self.assertEqual(result["failed"], [])

    def test_a_folder_moves_with_the_links_inside_it_untouched(self) -> None:
        self.write("Projects/Alpha/One.md", "[two](Two.md) [goals](../../Areas/Goals.md)\n")
        self.write("Projects/Alpha/Two.md", "[[One]]\n")
        self.write("Areas/Goals.md", "[one](../Projects/Alpha/One.md)\n")

        _, result = _move(self.root, self.root / "Projects/Alpha", "Archive")

        self.assertEqual(self.read("Archive/Alpha/One.md"), "[two](Two.md) [goals](../../Areas/Goals.md)\n")
        self.assertEqual(self.read("Archive/Alpha/Two.md"), "[[One]]\n")
        self.assertEqual(self.read("Areas/Goals.md"), "[one](../Archive/Alpha/One.md)\n")
        self.assertEqual(result["updated"], ["Areas/Goals.md"])

    def test_renaming_a_note_renames_its_name_links_and_keeps_its_extension(self) -> None:
        self.write("Areas/Old Name.md", "body\n")
        self.write("Projects/Index.md", "[[Old Name]] [[old name|alias]] ![[Old Name]] [md](../Areas/Old%20Name.md)\n")

        renamed, result = _rename(self.root, self.root / "Areas/Old Name.md", "New Name")

        self.assertEqual(renamed, self.root / "Areas/New Name.md")
        self.assertEqual(self.read("Projects/Index.md"), "[[New Name]] [[New Name|alias]] ![[New Name]] [md](../Areas/New%20Name.md)\n")
        self.assertEqual(result["links"], 4)

    def test_a_name_made_ambiguous_by_the_move_is_written_as_a_path(self) -> None:
        self.write("Areas/Notes.md", "areas\n")
        self.write("Projects/Notes.md", "projects\n")
        self.write("Projects/Alpha/Read.md", "[[Notes]]\n")  # Areas/Notes and Projects/Notes tie; the shallower-first sort picks Areas
        index = VaultIndex.build(self.root)
        self.assertEqual(index.resolve_wikilink("Notes", source=self.root / "Projects/Alpha/Read.md").rel, "Areas/Notes.md")

        _move(self.root, self.root / "Projects/Notes.md", "Projects/Alpha")  # now beside the note that links, so it would win

        self.assertEqual(self.read("Projects/Alpha/Read.md"), "[[Areas/Notes]]\n")

    def test_a_name_made_ambiguous_by_a_rename_is_written_as_a_path(self) -> None:
        # The note that links never names the file being renamed, only the name it takes, so the plan must not pass
        # it over for failing to mention what moved.
        self.write("Areas/Notes.md", "areas\n")
        self.write("Projects/Alpha/Draft.md", "draft\n")
        self.write("Projects/Alpha/Read.md", "[[Notes]] and `[[Notes]]`\n")

        _rename(self.root, self.root / "Projects/Alpha/Draft.md", "Notes")  # beside the link, so it would now win

        self.assertEqual(self.read("Projects/Alpha/Read.md"), "[[Areas/Notes]] and `[[Notes]]`\n")

    def test_notes_that_never_name_what_moved_are_not_scanned(self) -> None:
        self.write("Areas/Goals.md", "goals\n")
        self.write("Projects/Plan.md", "[the goals](../Areas/Goals.md)\n")
        self.write("Projects/Index.md", "[plan](Old%20Plan.md) [[Elsewhere]]\n")
        self.write("Other/Unrelated.md", "[[Projects/Index]] and nothing about it\n")
        index = VaultIndex.build(self.root)
        entry, moved = vault_mod.plan_move(self.root, self.root / "Areas/Goals.md", "Archive", "Notes")
        scanned: list[str] = []
        real = relink.rewrite_note

        def watch(text, before, after, moves, old_rel, new_rel):
            scanned.append(old_rel)
            return real(text, before, after, moves, old_rel, new_rel)

        with patch.object(relink, "rewrite_note", watch):
            planned = relink.plan(index, relink.file_moves(index, entry, moved))

        self.assertEqual(sorted(scanned), ["Areas/Goals.md", "Projects/Plan.md"])
        self.assertEqual([r.rel for r in planned.rewrites], ["Projects/Plan.md"])
        self.assertTrue(relink._names_any("[md](../Areas/Old%20Name.md)", {"old name"}))

    def test_an_edit_made_after_the_plan_is_never_overwritten(self) -> None:
        self.write("Areas/Goals.md", "[[Projects/Alpha/Plan]]\n")
        self.write("Projects/Alpha/Plan.md", "plan\n")
        index = VaultIndex.build(self.root)
        entry, moved = vault_mod.plan_move(self.root, self.root / "Projects/Alpha/Plan.md", "Archive", "Notes")
        planned = relink.plan(index, relink.file_moves(index, entry, moved))
        goals = self.root / "Areas/Goals.md"
        goals.write_text("[[Projects/Alpha/Plan]] and a new line typed meanwhile\n", encoding="utf-8")
        os.utime(goals, ns=(1, 1))  # a different version, even on a coarse clock
        vault_mod.move_entry(self.root, entry, "Archive", "Notes")

        result = relink.apply(self.root, planned)

        self.assertEqual(result["failed"], ["Areas/Goals.md"])
        self.assertIn("typed meanwhile", goals.read_text(encoding="utf-8"))

    def test_notes_in_a_linked_folder_are_reported_not_written(self) -> None:
        outside = Path(os.path.realpath(self._tmp.name)) / "elsewhere"
        outside.mkdir()
        (outside / "Ref.md").write_text("[[Projects/Alpha/Plan]]\n", encoding="utf-8")
        os.symlink(outside, self.root / "Linked")
        self.write("Projects/Alpha/Plan.md", "plan\n")

        _, result = _move(self.root, self.root / "Projects/Alpha/Plan.md", "Archive")

        self.assertEqual(result["skipped"], ["Linked/Ref.md"])
        self.assertEqual((outside / "Ref.md").read_text(encoding="utf-8"), "[[Projects/Alpha/Plan]]\n")


if __name__ == "__main__":
    unittest.main()
