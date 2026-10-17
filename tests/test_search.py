"""⌘P's passages (search.py): vault-mcp's index read in place, ranked by words and meaning, placed in Onyx's trees."""

from __future__ import annotations

import array
import math
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from onyx import search
from onyx.app import create_app
from onyx.config import AppConfig
from onyx.vault import VaultIndex

LAUNCHER_SWIFT = Path(__file__).resolve().parent.parent / "launcher" / "Onyx.swift"

# vault-mcp's tables as its store.py creates them (SCHEMA and FTS_SCHEMA). search.py reads them and never writes; if
# vault-mcp's schema changes, this is the copy that changes with it.
VAULT_MCP_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL, heading_path TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, tags, content='chunks', content_rowid='id', tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
END;
"""

# One direction of meaning per subject, so a stand-in embedder can put a query beside one of them.
DEPLOY, MUSIC, LAUNCH = [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]
NEUTRAL = [1, 1, 1, 1]  # 0.5 from every subject: under the floor, so meaning says nothing
PASSAGES = [
    ("Projects/Deploy.md", "Deploy > NAS", "The nas-tunnel carries every service out to the web.", DEPLOY),
    ("Areas/Music.md", "Music > Release", "Plan the release of the next single.", MUSIC),
    ("Artifacts/Pages/one.html", "Page one > Launch plan", "Friday it opens to everyone.", LAUNCH),
]


# The API's own corpus. Related scores against the corpus baseline, so these vectors have to mean something: Deploy
# and the Jev page share a subject, Page one is off on its own, and two pages sit elsewhere entirely to give the
# baseline somewhere to be. Orthogonal stand-ins would every one fall below the floor — which is what the floor is for.
# The fifth direction is nobody's, so a query about nothing in this corpus (API_NEUTRAL) stays under MEANING_FLOOR
# against every one of them, as NEUTRAL does for the orthogonal set above.
NEAR_A, NEAR_B = [1, 0, 0.6, 0, 0], [1, 0, 0.9, 0, 0]
APART, OFF, OFF_TOO = [0, 1, 0.3, 0, 0], [0, 0, 0, 1, 0], [0.3, 0, 0, 1, 0]
API_NEUTRAL = [0, 0, 0, 0, 1]

# Pages on no subject under test, so a related corpus has a middle for its baseline to sit in.
BULK = [
    ("Bulk/One.md", "One", "Filler about other matters entirely.", [0, 0, 1, 0]),
    ("Bulk/Two.md", "Two", "More filler, elsewhere again.", [0, 0, 0, 1]),
    ("Bulk/Three.md", "Three", "Filler, a third time.", [0, 0, 0.8, 0.6]),
]
API_PASSAGES = [
    ("Projects/Deploy.md", "Deploy > NAS", "The nas-tunnel carries every service out to the web.", NEAR_A),
    ("Artifacts/Jev.html", "Jev > What it is", "A typed judgment primitive.", NEAR_B),
    ("Artifacts/Pages/one.html", "Page one > Launch plan", "Friday it opens to everyone.", APART),
    ("Areas/Music.md", "Music > Release", "Plan the release of the next single.", OFF),
    ("Elsewhere.md", "", "The nas-tunnel, from a file no vault lists.", OFF_TOO),
]


def make_index(path: Path, passages, *, stamp: str = "2026-09-13T00:00:00+00:00") -> None:
    """A vault-mcp index at ``path`` holding ``passages``: (file_path, heading_path, text, vector) each."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(VAULT_MCP_SCHEMA)
        for file_path, heading, text, vector in passages:
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            conn.execute(
                "INSERT INTO chunks(file_path, heading_path, text, tags, embedding) VALUES (?, ?, ?, '', ?)",
                (file_path, heading, text, array.array("f", [x / norm for x in vector]).tobytes()),
            )
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [("model", "nomic-embed-text"), ("fts_version", "1"), ("last_index_time", stamp)],
        )
        conn.commit()


def stand_in(meanings: dict[str, list[float]], neutral: list[float] | None = None):
    """An embedder in Ollama's place: the vector of the first phrase the query holds, else ``neutral``."""

    def embed(model: str, text: str) -> list[float]:
        return next(
            (vector for phrase, vector in meanings.items() if phrase in text.lower()),
            NEUTRAL if neutral is None else neutral,
        )

    return embed


def as_notes(path: str) -> dict:
    return {"vault": "notes", "path": "/vault/" + path, "title": Path(path).stem, "folder": ""}


def found(result: dict) -> list[tuple[str, str]]:
    return [(item["title"], item["match"]) for item in result["items"]]


class PassageSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "index.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def index(self, passages, meanings: dict[str, list[float]] | None = None) -> search.PassageIndex:
        make_index(self.db, passages)
        return search.PassageIndex(self.db, embed=stand_in(meanings or {}))

    def test_what_is_typed_becomes_a_safe_match_with_the_last_word_as_a_prefix(self) -> None:
        self.assertEqual(search.fts_query("nas tunn"), '"nas" AND "tunn"*')
        self.assertEqual(search.fts_query("nas tunnel "), '"nas" AND "tunnel"')  # a space: the word is finished
        self.assertEqual(search.fts_query("nas-tunnel"), '"nas tunnel"*')
        self.assertEqual(search.fts_query('a:b "c'), '"a b" AND "c"*')  # nothing typed reaches FTS5's parser
        self.assertEqual(search.fts_query("  -- "), "")

    def test_words_find_the_passages_holding_every_word_the_last_as_typed_so_far(self) -> None:
        index = self.index(PASSAGES)
        result = index.search("nas tunn", place=as_notes)
        self.assertEqual(found(result), [("Deploy", "words")])
        self.assertEqual(result["words"], {"ok": True, "reason": ""})
        self.assertEqual(result["meaning"], {"ok": True, "reason": ""})
        self.assertEqual(index.search("nas release", place=as_notes)["items"], [])  # every word, in one passage

    def test_meaning_finds_a_page_that_shares_no_word_with_the_query(self) -> None:
        index = self.index(PASSAGES, {"shipping": [1, 0.2, 0, 0]})
        self.assertEqual(found(index.search("shipping it", place=as_notes)), [("Deploy", "meaning")])
        # Noise is still nearest to something; under the floor, that says nothing.
        self.assertEqual(index.search("xqzv", place=as_notes)["items"], [])

    def test_found_both_ways_ranks_first_and_says_so(self) -> None:
        passages = [
            ("Areas/Single.md", "Single", "Notes on the release of the single.", [0.3, 1, 0, 0]),
            ("Areas/Album.md", "Album", "Sequencing the album, track by track.", MUSIC),
        ]
        index = self.index(passages, {"release": MUSIC})
        self.assertEqual(found(index.search("release", place=as_notes)), [("Single", "both"), ("Album", "meaning")])

    def test_a_page_is_one_row_at_its_best_passage(self) -> None:
        passages = [
            (
                "Projects/Deploy.md",
                "Deploy > Overview",
                "Every service runs on the NAS, with backups, monitoring, logs, alerts, dashboards and much else.",
                DEPLOY,
            ),
            ("Projects/Deploy.md", "Deploy > Tunnel", "Service, service: the service tunnel.", DEPLOY),
        ]
        rows = self.index(passages).search("service", place=as_notes)["items"]
        self.assertEqual([(row["title"], row["heading"]) for row in rows], [("Deploy", "Tunnel")])

    def test_a_row_names_its_section_and_quotes_its_passage(self) -> None:
        text = "Intro. " * 40 + "The nas-tunnel carries every service. " + "More. " * 40
        passages = [("Projects/Deploy.md", "Deploy > The **NAS** > Tunnel [[setup|set-up]]", text, DEPLOY)]
        row = self.index(passages).search("nas-tunnel", place=as_notes)["items"][0]
        self.assertEqual(row["heading"], "Tunnel set-up")  # as the page shows it, which is how the reader finds it
        self.assertEqual(row["section"], "The NAS › Tunnel set-up")  # the page's own title isn't repeated
        self.assertIn("The nas-tunnel carries every service.", row["snippet"])
        self.assertTrue(row["snippet"].startswith("…") and row["snippet"].endswith("…"), row["snippet"])
        self.assertLessEqual(len(row["snippet"]), search.SNIPPET_LEN + 2)
        # Where a word starts, as the words leg matched it: "on" inside "rationale" is no place to begin.
        line = "Every service runs behind a single tunnel, and then the rationale follows."
        self.assertEqual(search.snippet(line, "on"), line)

    def test_a_passage_found_by_meaning_alone_is_quoted_from_its_start(self) -> None:
        text = "Every service runs behind a single tunnel, and the rota says who is on call when it drops."
        index = self.index([("Projects/Deploy.md", "Deploy", text, DEPLOY)], {"pager": DEPLOY})
        row = index.search("pager duty on call", place=as_notes)["items"][0]
        self.assertEqual((row["match"], row["snippet"]), ("meaning", text))

    def test_markdown_reads_as_the_page_shows_it(self) -> None:
        self.assertEqual(
            search.plain("See [[Beta|the beta note]], [docs](https://x.y) and **bold** _it_ in snake_case\n## Next"),
            "See the beta note, docs and bold it in snake_case Next",
        )

    def test_only_a_page_one_of_the_trees_lists_comes_back(self) -> None:
        base = Path(self.temp.name)
        notes, artifacts = base / "notes", base / "Artifacts"
        (notes / "Projects").mkdir(parents=True)
        (notes / "Projects" / "Deploy.md").write_text("# Deploy\n", encoding="utf-8")
        (artifacts / "Pages").mkdir(parents=True)
        (artifacts / "Pages" / "one.html").write_text("<title>Page one</title><p>x</p>", encoding="utf-8")
        (artifacts / "Guides" / "Setup").mkdir(parents=True)
        (artifacts / "Guides" / "Setup" / "index.html").write_text("<title>Set-up guide</title>", encoding="utf-8")
        trees = VaultIndex.build(notes, "notes"), VaultIndex.build(artifacts, "html")

        self.assertEqual(
            search.place("Projects/Deploy.md", *trees),
            {"vault": "notes", "path": str(notes / "Projects" / "Deploy.md"), "title": "Deploy", "folder": "Projects"},
        )
        self.assertEqual(
            search.place("Artifacts/Pages/one.html", *trees),
            {"vault": "html", "path": str(artifacts / "Pages" / "one.html"), "title": "Page one", "folder": "Pages"},
        )
        guide = search.place("Artifacts/Guides/Setup/index.html", *trees)  # a folder holding index.html is one page
        self.assertEqual((guide["title"], guide["folder"]), ("Set-up guide", "Guides"))
        for elsewhere in ("Projects/Gone.md", "../notes/Projects/Deploy.md", "Artifacts/../x.md", "Pages/one.html"):
            self.assertIsNone(search.place(elsewhere, *trees), elsewhere)
        self.assertIsNone(search.place("Projects/Deploy.md", None, trees[1]))  # Notes not set up

    def test_a_page_the_reader_is_showing_gets_vault_mcps_name_for_it(self) -> None:
        base = Path(self.temp.name)
        notes, artifacts = base / "notes", base / "Artifacts"
        (notes / "Projects").mkdir(parents=True)
        deploy = notes / "Projects" / "Deploy.md"
        deploy.write_text("# Deploy\n", encoding="utf-8")
        (artifacts / "Pages").mkdir(parents=True)
        real = base / "elsewhere" / "built.html"
        real.parent.mkdir(parents=True)
        real.write_text("<title>Built page</title>", encoding="utf-8")
        (artifacts / "Pages" / "one.html").symlink_to(real)  # Artifacts is a folder of links to HTML anywhere
        notes_tree, html_tree = VaultIndex.build(notes, "notes"), VaultIndex.build(artifacts, "html")

        self.assertEqual(search.locate(str(deploy), "notes", notes_tree), "Projects/Deploy.md")
        self.assertEqual(search.place("Projects/Deploy.md", notes_tree, html_tree)["path"], str(deploy))
        self.assertEqual(search.locate(str(artifacts / "Pages" / "one.html"), "html", html_tree), "Artifacts/Pages/one.html")
        # The reading history keys a page by the file it really is; the tree still lists it under its link.
        self.assertEqual(search.locate(str(real), "html", html_tree), "Artifacts/Pages/one.html")
        for outside in (str(base / "other.md"), str(notes), "", "Projects/Deploy.md"):
            self.assertIsNone(search.locate(outside, "notes", notes_tree), outside)
        self.assertIsNone(search.locate(str(deploy), "notes", None))  # Notes not set up

    def test_without_an_index_there_are_no_passages_and_it_says_where_it_looked(self) -> None:
        index = search.PassageIndex(self.db, embed=stand_in({}))
        result = index.search("tunnel", place=as_notes)
        self.assertEqual(result["items"], [])
        self.assertFalse(result["words"]["ok"])
        self.assertIn("no vault index at", result["words"]["reason"])
        self.assertIn("index.db", result["words"]["reason"])
        self.assertEqual(index.status(warm=True)["passages"], 0)

    def test_without_ollama_words_still_work_and_it_says_why_meaning_is_off(self) -> None:
        make_index(self.db, PASSAGES)
        with socket.socket() as sock:  # a port nothing listens on
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        index = search.PassageIndex(self.db, ollama=f"http://127.0.0.1:{port}")
        result = index.search("nas tunnel", place=as_notes)
        self.assertEqual(found(result), [("Deploy", "words")])
        self.assertEqual(result["meaning"], {"ok": False, "reason": "Ollama isn't running"})
        self.assertEqual(index.status(warm=True)["meaning"], {"ok": False, "reason": "Ollama isn't running"})

    def test_a_rebuilt_index_is_read_again(self) -> None:
        index = self.index(PASSAGES[:1])
        self.assertEqual(index.search("release", place=as_notes)["items"], [])
        make_index(self.db, PASSAGES[1:2], stamp="2026-09-14T00:00:00+00:00")  # vault-mcp's next reindex adds a page
        with patch.object(search, "RELOAD_EVERY", 0):
            self.assertEqual(found(index.search("release", place=as_notes)), [("Music", "words")])

    def test_a_newer_query_stops_an_older_ones_scan(self) -> None:
        index = self.index(PASSAGES)
        data = index._passages()
        index._latest = 2
        with self.assertRaises(search._Superseded):
            index._meaning(data, array.array("f", [1, 0, 0, 0]), 1)


class RelatedTests(unittest.TestCase):
    """The related pane (``PassageIndex.related``): the page being read, and the pages nearest it in meaning."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "index.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def index(self, passages) -> search.PassageIndex:
        # Filler, so the corpus baseline has somewhere to sit. Every score here is measured against it, so a corpus
        # of nothing but the pages under test would have its own subject as the baseline and cancel them out.
        make_index(self.db, passages + BULK)
        return search.PassageIndex(self.db, embed=stand_in({}))

    def test_the_nearest_pages_come_back_in_order_and_the_page_itself_never_does(self) -> None:
        passages = [
            ("Projects/Here.md", "Here", "The page being read.", [1, 0, 0, 0]),
            ("Projects/Here.md", "Here > More", "Still that page.", [1, 0.05, 0, 0]),
            ("Areas/Close.md", "Close", "Nearly the same subject.", [1, 0.25, 0, 0]),
            ("Areas/Far.md", "Far", "A related subject, further off.", [1, 0.6, 0, 0]),
        ]
        result = self.index(passages).related("Projects/Here.md", place=as_notes)
        self.assertEqual([item["title"] for item in result["items"]], ["Close", "Far"])
        self.assertEqual(result["note"], "Projects/Here.md")
        self.assertGreater(result["items"][0]["score"], result["items"][1]["score"])
        # A related scan embeds nothing — the page's own vectors are in the index — so Ollama being off can't stop one.
        def refuse(model: str, text: str):
            raise search.Unavailable("Ollama isn't running")

        off = search.PassageIndex(self.db, embed=refuse)
        self.assertEqual([i["title"] for i in off.related("Projects/Here.md", place=as_notes)["items"]], ["Close", "Far"])

    def test_a_page_is_one_row_at_its_nearest_passage(self) -> None:
        passages = [
            ("Areas/Here.md", "Here", "The page being read.", MUSIC),
            ("Areas/There.md", "There > Far", "Nothing to do with it.", DEPLOY),
            ("Areas/There.md", "There > Near", "All about the release.", [0.1, 1, 0, 0]),
        ]
        rows = self.index(passages).related("Areas/Here.md", place=as_notes)["items"]
        self.assertEqual([(row["title"], row["heading"]) for row in rows], [("There", "Near")])
        self.assertGreater(rows[0]["score"], 0.99)  # its nearest passage, not its average

    def test_the_page_is_the_average_of_its_own_passages(self) -> None:
        # Two passages pointing different ways average to a direction between them, which is nearest the page between.
        passages = [
            ("Areas/Both.md", "Both > A", "Deploying things.", DEPLOY),
            ("Areas/Both.md", "Both > B", "Making music.", MUSIC),
            ("Areas/Mix.md", "Mix", "Deploying things while making music.", [1, 1, 0, 0]),
            ("Areas/One.md", "One", "Only deploying.", DEPLOY),  # half the subject, so further out
        ]
        rows = self.index(passages).related("Areas/Both.md", place=as_notes)["items"]
        self.assertEqual([row["title"] for row in rows], ["Mix", "One"])

    def test_a_note_beside_its_own_rendering_is_marked_though_it_scores_under_the_bar(self) -> None:
        """The commonest real duplicate, and the one the score alone misses: one document, two formats."""
        passages = [
            ("Resources/Jev.md", "Jev", "The source of truth for the Jev primitive.", [1, 0, 0, 0]),
            ("Artifacts/Resources/Jev/Jev.html", "Jev", "The Jev primitive, rendered to read.", [1, 0.25, 0, 0]),
            ("Resources/Jev in zen-mcp.md", "Jev in zen-mcp", "Where Jev is wired in.", [1, 0.6, 0, 0]),
        ]
        rows = self.index(passages).related("Resources/Jev.md", place=as_notes)["items"]
        marked = {row["title"]: row["dupe"] for row in rows}
        self.assertTrue(marked["Jev"])  # one name, two formats — and below DUPE_AT, so only the name says so
        self.assertLess(dict((r["title"], r["score"]) for r in rows)["Jev"], search.DUPE_AT)
        self.assertFalse(marked["Jev in zen-mcp"])  # its own document, however near it reads

    def test_a_name_the_vault_reuses_is_a_filing_habit_and_never_marks_anything(self) -> None:
        # Every client folder holds a proposal.typ. They are different proposals, and the name is no evidence at all.
        passages = [
            ("Clients/Alpha/proposal.typ", "Alpha", "Scope, price and terms for this engagement.", [1, 0, 0, 0]),
            ("Clients/Beta/proposal.typ", "Beta", "Scope, price and terms for a different one.", [1, 0.4, 0, 0]),
            ("Clients/Gamma/proposal.typ", "Gamma", "Scope, price and terms again.", [1, 0.6, 0, 0]),
        ]
        rows = self.index(passages).related("Clients/Alpha/proposal.typ", place=as_notes)["items"]
        self.assertTrue(rows)
        # Every one sits in the band where a name would mark it, and the name is the only thing saying so.
        for row in rows:
            self.assertTrue(search.DUPE_NAMED <= row["score"] < search.DUPE_AT, row)
            self.assertFalse(row["dupe"], row)

    def test_a_document_is_named_by_its_file_without_its_format_or_a_copys_tail(self) -> None:
        for path, name in (
            ("Resources/Jev.md", "jev"),
            ("Artifacts/Resources/Jev/Jev.html", "jev"),
            ("Meetings/Team Sync 04.28.26 2.md", "team sync 04.28.26"),
            ("Meetings/Team Sync 04.28.26 copy.md", "team sync 04.28.26"),
            ("Notes/report (1).md", "report"),
            ("Notes/no-extension", "no-extension"),
        ):
            self.assertEqual(search.document_name(path), name, path)

    def test_a_near_enough_neighbour_is_marked_as_the_same_page_twice(self) -> None:
        passages = [
            ("Inbox/Capture.md", "Capture", "The raw capture of the note.", DEPLOY),
            ("Projects/Filed.md", "Filed", "The filed version of the same thing.", [1, 0.03, 0, 0]),
            ("Areas/Other.md", "Other", "On its subject, but not it.", [1, 1, 0, 0]),
        ]
        rows = self.index(passages).related("Inbox/Capture.md", place=as_notes)["items"]
        self.assertEqual([(row["title"], row["dupe"]) for row in rows], [("Filed", True), ("Other", False)])
        self.assertGreaterEqual(rows[0]["score"], search.DUPE_AT)
        self.assertLess(rows[1]["score"], search.DUPE_AT)

    def test_a_page_near_everything_stops_crowding_out_the_pages_that_are_actually_near(self) -> None:
        """The hub problem, which is why a bare cosine can't carry a floor.

        ``Hub`` sits in the middle of the corpus and so scores well against anything; ``Kin`` is off to one side with
        the page being read. Ranked by bare cosine the hub wins, which is what put meeting notes beside a tmux note.
        """
        passages = [
            ("Areas/Here.md", "Here", "The page being read: its own subject, in general terms.", [1, 0.5, 0.5, 0.5]),
            ("Areas/Hub.md", "Hub", "A long, diffuse playbook touching everything.", [1, 1, 1, 1]),
            ("Areas/Kin.md", "Kin", "The same narrow subject, and only it.", [1, 0, 0, 0]),
            ("Bulk/A.md", "A", "Filler that sets where the middle of this corpus is.", [1, 1, 1, 1]),
            ("Bulk/B.md", "B", "More of the same filler.", [1, 1, 1, 0.95]),
            ("Bulk/C.md", "C", "Still more filler.", [1, 0.95, 1, 1]),
        ]
        make_index(self.db, passages)
        index = search.PassageIndex(self.db, embed=stand_in({}))
        data = index._passages()
        here = data.vecs[data.pages["Areas/Here.md"][0]]
        bare = {p: search._dot(here, data.vecs[data.pages[p][0]]) for p in ("Areas/Hub.md", "Areas/Kin.md")}
        self.assertGreater(bare["Areas/Hub.md"], bare["Areas/Kin.md"])  # the bare cosine prefers the hub
        ranked = [item["title"] for item in index.related("Areas/Here.md", place=as_notes, limit=9)["items"]]
        self.assertEqual(ranked[0], "Kin")  # with the baseline out, the page that shares the subject wins
        self.assertNotIn("Hub", ranked)  # and the hub drops under the floor rather than merely down the list

    def test_the_map_is_told_how_related_the_neighbours_are_to_one_another(self) -> None:
        """``near``: what lets the map put the neighbours on one subject together, in whatever order the scores list them."""
        passages = [
            ("Areas/Here.md", "Here", "The page being read, on two subjects.", [1, 1, 0, 0]),
            ("Areas/DeployA.md", "DeployA", "Deploying, one way.", [1, 0.2, 0, 0]),
            ("Areas/DeployB.md", "DeployB", "Deploying, another way.", [1, 0.3, 0, 0.05]),
            ("Areas/MusicA.md", "MusicA", "Music, one way.", [0.2, 1, 0, 0]),
            ("Areas/MusicB.md", "MusicB", "Music, another way.", [0.3, 1, 0.05, 0]),
        ]
        index = self.index(passages)
        result = index.related("Areas/Here.md", place=as_notes)
        titles = [item["title"] for item in result["items"]]
        # The scores interleave the two subjects, so any layout by rank alone would pull each pair apart.
        self.assertEqual(titles, ["DeployB", "MusicB", "DeployA", "MusicA"])
        near = {(a, b): result["near"][i][j] for i, a in enumerate(titles) for j, b in enumerate(titles)}
        for a in titles:
            self.assertEqual(near[a, a], 1.0)
            for b in titles:
                self.assertEqual(near[a, b], near[b, a])  # one distance per pair, so one number
                if a != b and a[:5] == b[:5]:
                    for other in (t for t in titles if t[:5] != a[:5]):
                        self.assertGreater(near[a, b], near[a, other], (a, b, other))
        # The map draws only the first RELATED_MAP, so that is all the table covers; and one neighbour has no pairs.
        with patch.object(search, "RELATED_MAP", 2):
            self.assertEqual(len(index.related("Areas/Here.md", place=as_notes)["near"]), 2)
        with patch.object(search, "RELATED_MAP", 1):
            self.assertEqual(len(index.related("Areas/Here.md", place=as_notes, limit=1)["near"]), 0)

    def test_the_floor_leaves_out_the_tail_and_a_page_with_no_neighbour_comes_back_empty(self) -> None:
        passages = [
            ("Areas/Alone.md", "Alone", "A subject in this vault exactly once.", [1, 0, 0, 0]),
            ("Areas/Bulk1.md", "Bulk", "Filler about other things.", [0, 1, 1, 1]),
            ("Areas/Bulk2.md", "Bulk", "More filler about other things.", [0, 1, 1, 0.9]),
            ("Areas/Bulk3.md", "Bulk", "Still more filler.", [0, 0.9, 1, 1]),
        ]
        result = self.index(passages).related("Areas/Alone.md", place=as_notes)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["reason"], "")  # indexed, and simply has nothing near it — not a failure
        self.assertEqual(result["floor"], search.RELATED_FLOOR)

    def test_the_corpus_baseline_is_the_same_whichever_way_the_index_is_read(self) -> None:
        # A stride sample, so the same index always gives the same baseline, and so the same scores.
        vecs = [array.array("f", v) for v in ([1, 0, 0, 0], [0.9, 0.3, 0, 0], [1, 0.1, 0, 0], [0.8, 0.5, 0, 0])]
        mu = search.centroid(vecs)
        self.assertAlmostEqual(math.sqrt(search._dot(mu, mu)), 1.0, places=5)
        self.assertEqual(list(mu), list(search.centroid(vecs)))
        self.assertEqual(len(search.centroid(vecs, cap=2)), 4)  # a smaller sample still spans the dimensions
        self.assertEqual(len(search.centroid([])), 0)

    def test_a_score_of_zero_means_no_closer_than_any_two_pages(self) -> None:
        # Against the baseline, a page sitting exactly at it scores 0, and the same text scores 1.
        self.assertAlmostEqual(search.against_baseline(raw=1.0, source_mu=0.5, page_mu=0.5), 1.0, places=6)
        self.assertAlmostEqual(search.against_baseline(raw=0.0, source_mu=0.5, page_mu=0.5), 0.0, places=6)
        self.assertEqual(search.against_baseline(raw=1.0, source_mu=1.0, page_mu=1.0), 0.0)  # no spread, no answer

    def test_a_page_the_index_doesnt_hold_says_so_rather_than_coming_back_empty(self) -> None:
        index = self.index(PASSAGES)
        for missing in ("Projects/Nothing.md", "", "Artifacts/Pages/gone.html"):
            result = index.related(missing, place=as_notes)
            self.assertEqual(result["items"], [], missing)
            self.assertIn("isn't in the vault index", result["reason"], missing)
        # Case is the one thing that may differ: the tree reads a name off the disk, and so does vault-mcp.
        self.assertEqual(index.related("projects/deploy.MD", place=as_notes)["reason"], "")

    def test_without_an_index_it_says_where_it_looked(self) -> None:
        result = search.PassageIndex(self.db).related("Projects/Deploy.md", place=as_notes)
        self.assertEqual(result["items"], [])
        self.assertIn("no vault index at", result["reason"])

    def test_a_newer_page_stops_an_older_ones_scan_without_touching_the_palettes(self) -> None:
        index = self.index(PASSAGES)
        index._passages()
        real = search._dot

        def opened_another(a, b):  # a page opened in the reader while this scan was still running
            index._latest_related += 1
            return real(a, b)

        with patch.object(search, "SCAN_BLOCK", 1), patch.object(search, "_dot", opened_another):
            result = index.related("Projects/Deploy.md", place=as_notes)
        self.assertTrue(result["superseded"])
        self.assertEqual(result["items"], [])
        self.assertEqual(index._latest, 0)  # the palette's own scans are counted apart, and were left alone


class SearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.notes, self.artifacts = base / "notes", base / "Artifacts"
        (self.notes / "Projects").mkdir(parents=True)
        self.deploy = self.notes / "Projects" / "Deploy.md"
        self.deploy.write_text("# Deploy\n\n## NAS\n\nThe nas-tunnel carries every service out to the web.\n", encoding="utf-8")
        (self.artifacts / "Pages").mkdir(parents=True)
        self.one = self.artifacts / "Pages" / "one.html"
        self.one.write_text("<title>Page one</title><h2>Launch plan</h2><p>Friday it opens to everyone.</p>", encoding="utf-8")
        # A vault HTML note linked into Artifacts: one file, and so a row in both trees. vault-mcp leaves out a vault
        # HTML file that renders a same-name .md, so only its Artifacts name is in the index.
        (self.notes / "Resources").mkdir(parents=True)
        self.shared = self.notes / "Resources" / "Jev.html"
        self.shared.write_text("<title>Jev</title><h2>What it is</h2><p>A typed judgment.</p>", encoding="utf-8")
        self.link = self.artifacts / "Jev.html"
        self.link.symlink_to(self.shared)
        db = base / "index.db"
        # Music.md is in the index but in neither vault, and so is a page no vault lists: neither comes back.
        make_index(db, API_PASSAGES)
        config = AppConfig(default_folder=base, allowed_roots=(base,), port=8899, data_dir=base / "data")
        self.app = create_app(config)
        self.app.state.storage.update_settings(
            {"vault_root": str(self.notes), "html_vault_root": str(self.artifacts)}, model_default=config.model
        )
        self.app.state.passages = search.PassageIndex(db, embed=stand_in({"going live": APART}, API_NEUTRAL))
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_passages_come_back_as_the_trees_list_their_pages(self) -> None:
        result = self.client.get("/api/search", params={"q": "nas tunn"}).json()
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["items"],
            [
                {
                    "vault": "notes",
                    "path": str(self.deploy),
                    "title": "Deploy",
                    "folder": "Projects",
                    "heading": "NAS",
                    "section": "NAS",
                    "snippet": "The nas-tunnel carries every service out to the web.",
                    "match": "words",
                }
            ],
        )
        live = self.client.get("/api/search", params={"q": "going live"}).json()["items"]
        self.assertEqual(
            [(i["vault"], i["path"], i["title"], i["folder"], i["heading"], i["match"]) for i in live],
            [("html", str(self.one), "Page one", "Pages", "Launch plan", "meaning")],
        )

    def test_status_says_what_a_search_would_use(self) -> None:
        status = self.client.get("/api/search/status").json()
        self.assertEqual(
            (status["ok"], status["words"]["ok"], status["meaning"]["ok"], status["passages"]), (True, True, True, 5)
        )

    def test_related_places_the_neighbours_of_the_page_being_read(self) -> None:
        result = self.client.get("/api/related", params={"vault": "notes", "path": str(self.deploy)}).json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "Projects/Deploy.md")
        # The pages a tree lists that are above the floor, at their nearest passage. The page itself, the file no
        # vault lists, and the page that is merely in the same vault are all left out; the linked page comes back as
        # the Artifacts row, which is the name it is indexed under.
        self.assertEqual(
            [(i["vault"], i["path"], i["title"], i["heading"]) for i in result["items"]],
            [("html", str(self.link), "Jev", "What it is")],
        )
        # An artifact is asked for the same way, and reaches vault-mcp under its mount.
        artifact = self.client.get("/api/related", params={"vault": "html", "path": str(self.link)}).json()
        self.assertEqual(artifact["note"], "Artifacts/Jev.html")
        self.assertEqual([i["title"] for i in artifact["items"]], ["Deploy"])
        # The vault named is only which tree to ask first: a page opened before the shell's trees arrive can be named
        # by the wrong one, and the page it is still decides the answer.
        for guess in ("notes", "html", "nonsense"):
            asked = self.client.get("/api/related", params={"vault": guess, "path": str(self.one)}).json()
            self.assertEqual(asked["note"], "Artifacts/Pages/one.html", guess)

    def test_a_page_with_nothing_near_it_comes_back_empty_rather_than_padded(self) -> None:
        # Page one is in the index and in a tree; it simply shares its subject with nothing else here.
        result = self.client.get("/api/related", params={"vault": "html", "path": str(self.one)}).json()
        self.assertEqual((result["ok"], result["items"], result["reason"]), (True, [], ""))
        self.assertEqual(result["floor"], search.RELATED_FLOOR)

    def test_a_page_in_both_vaults_is_asked_for_by_the_name_the_index_holds(self) -> None:
        # Its Notes name ("Resources/Jev.html") is deliberately not indexed, so a guess that lands there finds nothing.
        for named in (str(self.link), str(self.shared)):
            for guess in ("notes", "html"):
                result = self.client.get("/api/related", params={"vault": guess, "path": named}).json()
                self.assertEqual(result["note"], "Artifacts/Jev.html", (named, guess))
                self.assertTrue(result["items"], (named, guess))

    def test_related_says_so_for_a_page_no_vault_lists(self) -> None:
        for path in ("", str(Path(self.temp.name) / "loose.html"), "https://example.com/x"):
            result = self.client.get("/api/related", params={"vault": "notes", "path": path}).json()
            self.assertEqual((result["ok"], result["items"], result["cut"]), (True, [], 0), path)
            self.assertIn("isn't in Notes or Artifacts", result["reason"], path)

    def test_both_routes_refuse_another_origin(self) -> None:
        for route in ("/api/search?q=nas", "/api/search/status", "/api/related?path=x"):
            response = self.client.get(route, headers={"origin": "https://attacker.example"})
            self.assertEqual(response.status_code, 403, route)

    def test_the_shell_carries_the_palette_and_the_app_menu_opens_it(self) -> None:
        page = self.client.get("/").text
        self.assertIn("<dialog id=search-modal", page)
        self.assertIn("openSearch:()=>SEARCH.open()", page)
        # File ▸ Search… (⌘P) calls the entry point the shell exposes, or loads the fragment that opens it.
        swift = LAUNCHER_SWIFT.read_text(encoding="utf-8")
        self.assertIn('menuItem("Search…", #selector(openSearch), "p")', swift)
        self.assertIn('shellCall("onyxShell.openSearch()", fallback: "/#search")', swift)

    def test_the_shell_carries_the_panels_switch_and_the_related_pane(self) -> None:
        page = self.client.get("/").text
        for mark in ("<button id=tab-outline", "<button id=tab-related", "<div id=related", "<svg id=rel-map"):
            self.assertIn(mark, page, mark)


if __name__ == "__main__":
    unittest.main()
