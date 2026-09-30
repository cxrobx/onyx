"""The phone's Library home: recent pages and recent asks, published only for pages the mirror publishes.

Plan: docs/plans/phone-mirror.md, "Library". The publisher round trip needs the mirror extra and skips without it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from onyx.mirror import config as mirror_config
from onyx.mirror.build import PAGE_MIME, BuiltObject, MirrorBuild
from onyx.mirror.library import MAX_RECENT, build_library

try:
    import cryptography  # noqa: F401

    HAVE_EXTRA = True
except ImportError:
    HAVE_EXTRA = False


def ids(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def page(path: str, title: str) -> BuiltObject:
    name = f"page:{path}"
    return BuiltObject(name=name, id=ids(name), data=b"<p>x</p>", mime=PAGE_MIME,
                       page={"path": path, "title": title, "kind": "markdown", "mtime": 0.0, "text": "x"})


def conversation(rid: str, source: str, *, started: float, parent: str | None = None, status: str = "complete",
                 question: str = "What does this mean?", answer: str = "It means **this**.",
                 action: str = "ask") -> dict:
    return {"request_id": rid, "document_source": source, "document_title": "Doc", "selection": "the passage",
            "action": action, "question": question, "answer": answer, "model": "sonnet", "status": status,
            "parent_request_id": parent, "started_at": started}


class FakeHistory:
    """The two Storage reads build_library makes, by their real names."""

    def __init__(self, documents: list[dict], conversations: list[dict]) -> None:
        self.documents, self.conversations = documents, conversations

    def recent_documents(self, limit: int = 20) -> list[dict]:
        return self.documents[:limit]

    def recent_conversations(self, *, limit: int = 50, **_) -> list[dict]:
        return self.conversations[:limit]


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        (self.base / "notes").mkdir()
        self.shown = self.base / "notes" / "shown.md"
        self.hidden = self.base / "clients" / "secret.md"
        self.shown.write_text("# Shown\n")
        self.hidden.parent.mkdir()
        self.hidden.write_text("# Secret\n")
        # An Artifacts entry is a link; history keys the page by the file it points at.
        self.link = self.base / "artifacts-link.html"
        self.target = self.base / "guide.html"
        self.target.write_text("<title>Guide</title>")
        self.link.symlink_to(self.target)

        self.built = MirrorBuild()
        self.built.extend([page("Notes/shown.md", "Shown"), page("Artifacts/guide.html", "Guide")])
        self.shown_id, self.guide_id = self.built[0].id, self.built[1].id
        self.built.pages_by_real = {str(self.shown): self.shown_id, str(self.target): self.guide_id}

    def library(self, history: FakeHistory, *, chats: bool = True):
        return build_library(history, built=self.built, ids=ids, look=None, chats=chats)

    def test_mirror_recent_lists_only_published_pages(self) -> None:
        history = FakeHistory([
            {"source": str(self.shown), "last_opened_at": 30.0},
            {"source": str(self.hidden), "last_opened_at": 20.0},        # a file outside the mirror
            {"source": "https://example.com/page", "last_opened_at": 15.0},
            {"source": str(self.link), "last_opened_at": 10.0},          # resolves to the guide
            {"source": str(self.shown), "last_opened_at": 5.0},          # already listed
        ], [])
        recent = self.library(history).library["recent"]
        self.assertEqual(recent, [{"id": self.shown_id, "opened_at": 30.0}, {"id": self.guide_id, "opened_at": 10.0}])
        many = FakeHistory([{"source": str(self.shown), "last_opened_at": float(i)} for i in range(5)]
                           + [{"source": str(self.target), "last_opened_at": 1.0}], [])
        self.assertLessEqual(len(self.library(many).library["recent"]), MAX_RECENT)

    def test_mirror_chats_stay_home_without_the_switch(self) -> None:
        history = FakeHistory([], [conversation("r1", str(self.shown), started=1.0)])
        off = self.library(history, chats=False)
        self.assertEqual((off.pages, off.library["chats"]), ([], []))
        # The switch reads only a literal true, as `enabled` does.
        with tempfile.TemporaryDirectory() as data:
            for value, expected in (("true", True), ('"true"', False), ("1", False), (None, False)):
                line = f"chats = {value}\n" if value is not None else ""
                (Path(data) / "mirror.toml").write_text(f'enabled = true\ninclude = ["Notes"]\n{line}')
                self.assertIs(mirror_config.load(Path(data)).chats, expected, value)

    def test_mirror_publishes_only_completed_asks_on_published_pages(self) -> None:
        history = FakeHistory([], [
            conversation("f1", str(self.shown), started=3.0, parent="r1", question="And then?"),
            conversation("r1", str(self.shown), started=1.0),
            conversation("c1", str(self.shown), started=4.0, status="cancelled"),
            conversation("x1", str(self.hidden), started=5.0, question="About the client"),
            conversation("g1", str(self.link), started=2.0, action="eli5", question=""),
        ])
        shelf = self.library(history)
        chats = shelf.library["chats"]
        self.assertEqual([(c["doc"], c["turns"], c["action"]) for c in chats],
                         [(self.shown_id, 2, "ask"), (self.guide_id, 1, "eli5")])
        self.assertEqual(chats[0]["updated_at"], 3.0)
        self.assertEqual(chats[1]["title"], "Explain like I'm 5: the passage")
        self.assertEqual({p.page["path"] for p in shelf.pages}, {"Chats/r1", "Chats/g1"})
        self.assertTrue(all(p.page["kind"] == "chat" for p in shelf.pages))
        everything = b"".join(p.data for p in shelf.pages) + json.dumps(shelf.library).encode()
        self.assertNotIn(b"About the client", everything)
        thread = next(p for p in shelf.pages if p.page["path"] == "Chats/r1").data.decode()
        self.assertIn(f'href="{self.shown_id}"', thread, "a thread links back to the page it was asked on")
        self.assertLess(thread.index("What does this mean?"), thread.index("And then?"))

    def test_mirror_answers_cannot_run_script(self) -> None:
        answer = ("<script>alert(1)</script> <img src=x onerror=alert(2)> [bad](javascript:alert(3)) "
                  "[web](https://example.com/a) **bold**")
        history = FakeHistory([], [conversation("r1", str(self.shown), started=1.0, answer=answer)])
        html = self.library(history).pages[0].data.decode()
        # The page's one script is the renderer; the answer rides as escaped text in its card.
        self.assertEqual(html.count("<script"), 1)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is needed to run the renderer the phone runs")
        # The renderer the phone runs, on the text the card carries (textContent is the unescaped answer).
        from onyx import panels_ui

        script = f"const md={panels_ui.answer_markdown()};process.stdout.write(md(require('fs').readFileSync(0,'utf8')))"
        out = subprocess.run([node, "-e", script], input=answer, capture_output=True, text=True, check=True).stdout
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn('href="javascript:', out)
        self.assertIn('href="https://example.com/a"', out)
        self.assertIn("<strong>bold</strong>", out)

    def test_mirror_threads_draw_as_the_macs_saved_answers(self) -> None:
        from onyx import vault_look

        history = FakeHistory([], [
            conversation("r1", str(self.shown), started=1.0),
            conversation("f1", str(self.shown), started=2.0, parent="r1", question="And then?"),
        ])
        look = vault_look.palette({"mode": "dark", "styles": {"content": {"background-color": "rgb(26, 26, 26)",
                                                                          "color": "rgb(220, 220, 220)"}}}, None)
        html = build_library(history, built=self.built, ids=ids, look=look, chats=True).pages[0].data.decode()
        self.assertEqual(html.count('<article class="turn">'), 2)
        self.assertEqual(html.count("<h4>Passage</h4>"), 1, "a follow-up on the same passage doesn't repeat it")
        self.assertEqual(html.count("<h4>Question</h4>"), 2)
        self.assertEqual(html.count("<h4>Answer</h4>"), 2)
        self.assertIn('<span class="badge">Question</span>', html)
        self.assertIn(f'href="{self.shown_id}">Open document</a>', html)
        self.assertIn("<title>Shown</title>", html, "titled by its document, as the Mac's detail is")
        self.assertIn(f"--bg-primary:{look['tokens']['--bg-primary']}", html)
        self.assertIn("color-scheme:dark", html)
        self.assertIn(".hist-answer", html, "the Mac's card rules, from panels_ui")

    def test_mirror_library_build_is_deterministic(self) -> None:
        history = FakeHistory([{"source": str(self.shown), "last_opened_at": 1.0}],
                              [conversation("r1", str(self.shown), started=1.0)])
        first, second = self.library(history), self.library(history)
        self.assertEqual([p.data for p in first.pages], [p.data for p in second.pages])
        self.assertEqual(first.library, second.library)


@unittest.skipUnless(HAVE_EXTRA, "needs the mirror extra: pip install onyx[mirror]")
class LibraryPublishTests(unittest.TestCase):
    def test_opening_a_page_resends_only_the_library(self) -> None:
        from onyx.mirror.crypto import Keys, inflate_raw
        from onyx.mirror.publish import publish
        from onyx.mirror.secrets import MemorySecrets
        from onyx.mirror.store import LocalDirStore

        vectors = json.loads((Path(__file__).parent / "fixtures" / "mirror_vectors.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            ops: list[tuple[str, str]] = []

            class Store(LocalDirStore):
                def put(self, id: str, data: bytes) -> None:
                    ops.append(("put", id))
                    super().put(id, data)

            store, data = Store(base / "out"), base / "data"
            data.mkdir()
            shelf = {"recent": [{"id": "a" * 64, "opened_at": 1.0}]}

            def build(*, roots, include, exclude, ids, markdown_css=None):
                built = MirrorBuild()
                built.append(BuiltObject(name="page:Notes/a.md", id=ids("page:Notes/a.md"), data=b"<p>a</p>",
                                         mime=PAGE_MIME, page={"path": "Notes/a.md", "title": "a", "kind": "markdown",
                                                               "mtime": 0.0, "text": "a"}))
                return built

            def library(built, ids):
                from onyx.mirror.library import LibraryBuild

                return LibraryBuild(pages=[], library={"v": 1, **shelf, "chats": []})

            config = mirror_config.MirrorConfig(enabled=True, interval_minutes=5, include=("Notes",), exclude=())
            secrets = MemorySecrets({"master_key": vectors["master_key_b64url"]})

            def run() -> None:
                publish(config=config, secrets=secrets, store=store, roots={"Notes": base}, markdown_css=None,
                        data_dir=data, build=build, now=datetime(2026, 9, 30, tzinfo=timezone.utc), library=library)

            keys = Keys.from_b64url(vectors["master_key_b64url"])
            run()
            library_id = keys.ident("library")
            self.assertEqual(ops[-1], ("put", library_id), "the library goes up after the index")
            self.assertLess(ops.index(("put", keys.ident("index"))), ops.index(("put", library_id)))
            stored = json.loads(inflate_raw(keys.open_blob(library_id, (store.dir / "o" / library_id).read_bytes())))
            self.assertEqual(stored["recent"], shelf["recent"])
            ops.clear()
            run()
            self.assertEqual(ops, [], "nothing changed, nothing sent")
            shelf["recent"] = [{"id": "a" * 64, "opened_at": 2.0}]
            run()
            self.assertEqual(ops, [("put", library_id)], "a new open re-sends the library alone")
            self.assertTrue((store.dir / "o" / library_id).exists(), "the stale sweep never takes the library")


if __name__ == "__main__":
    unittest.main()
