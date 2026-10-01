from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from onyx.app import create_app
from onyx.claude_runner import _sse
from onyx.config import AppConfig
from onyx.panels_ui import answer_markdown
from onyx.storage import Storage


def make_vault(base: Path) -> tuple[Path, Path]:
    vault = base / "vault"
    outside = base / "outside"
    (vault / "notes").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    outside.mkdir()
    (vault / "notes" / "Alpha.md").write_text(
        "---\ntags: [career]\n---\n\n# Alpha\n\nSee [[Beta]] and [[Nowhere]].\n", encoding="utf-8"
    )
    (vault / "Beta.md").write_text("# Beta\n", encoding="utf-8")
    (vault / ".obsidian" / "workspace.md").write_text("# Hidden\n", encoding="utf-8")
    (outside / "L.md").write_text("# Linked\n\n[[M]]\n", encoding="utf-8")
    (outside / "M.md").write_text("# M\n", encoding="utf-8")
    (vault / "linked").symlink_to(outside, target_is_directory=True)
    return vault, outside


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document = self.root / "guide.md"
        self.document.write_text("# Guide\n\nThe server is local.", encoding="utf-8")
        self.document = self.document.resolve()
        self.config = AppConfig(
            default_folder=self.root,
            allowed_roots=(self.root,),
            port=8899,
            data_dir=self.root / "data",
        )
        self.app = create_app(self.config)
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_highlights_are_scoped_to_a_page_and_mutations_require_the_token(self) -> None:
        source = str(self.document)
        body = {"document_source": source, "selection": "The server is local.",
                "context": "The server is local.", "prefix": "", "suffix": ""}
        self.assertEqual(self.client.post("/api/highlights", json=body).status_code, 403)
        self.assertEqual(self.client.get("/api/highlights", params={"source": source}).json()["highlights"], [])

        created = self.client.post("/api/highlights", json={**body, "token": self.config.token})
        self.assertEqual(created.status_code, 200)
        item = created.json()["highlight"]
        self.assertEqual(item["selection"], body["selection"])
        self.assertEqual(len(self.client.get("/api/highlights", params={"source": source}).json()["highlights"]), 1)
        self.assertEqual(self.client.get("/api/highlights", params={"source": "another.md"}).json()["highlights"], [])

        url = "/api/highlights/" + item["id"]
        self.assertEqual(self.client.patch(url, json={"note": "Remember this"}).status_code, 403)
        updated = self.client.patch(url, json={"token": self.config.token, "note": "Remember this"})
        self.assertEqual(updated.json()["highlight"]["note"], "Remember this")
        self.assertEqual(self.client.request("DELETE", url, json={}).status_code, 403)
        self.assertEqual(self.client.request("DELETE", url, json={"token": self.config.token}).status_code, 200)
        self.assertEqual(self.client.get("/api/highlights", params={"source": source}).json()["highlights"], [])

    def test_sidebar_logo_is_the_transparent_gem(self) -> None:
        mark = self.client.get("/onyx-mark.png")
        self.assertEqual(mark.status_code, 200)
        self.assertEqual(mark.headers["content-type"], "image/png")
        self.assertEqual(mark.content[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(mark.content[25], 6)  # IHDR colour type 6: RGBA, so no background
        self.assertIn('<img class=mark src=/onyx-mark.png alt="">', self.client.get("/").text)

    def test_the_error_page_goes_back_to_library_in_the_whole_window(self) -> None:
        # It shows in the shell's reader frame; a plain link there would load the whole shell inside the frame.
        refused = self.client.get("/view", params={"src": "relative/notes.md"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("<a href='/' target='_top'", refused.text)
        self.assertIn("back to Library", refused.text)
        self.assertNotIn("launcher", refused.text)

    def test_library_settings_and_document_capabilities(self) -> None:
        launcher = self.client.get("/")
        self.assertIn("Window transparency", launcher.text)
        self.assertIn("--pane-alpha", launcher.text)
        self.assertIn("Subscription provider", launcher.text)
        for select_id in (
            "history-provider",
            "history-model",
            "history-document",
            "history-date",
            "appearance-theme",
            "provider",
            "model",
            "reasoning-effort",
            "response-style",
        ):
            self.assertIn(f"<select id={select_id}", launcher.text)
        self.assertIn("-webkit-appearance:menulist", launcher.text)
        self.assertNotIn('role="listbox"', launcher.text)
        self.assertIn("background:rgb(var(--button-bg))", launcher.text)
        self.assertNotIn(".primary{border:0;background:rgb(var(--accent))", launcher.text)
        # The launcher's pages are the shell's two dialogs now, and the shell opens on Library.
        self.assertIn('<body class="kind-library', launcher.text)
        self.assertIn('<dialog id=settings-modal', launcher.text)
        self.assertIn("Recent conversations", launcher.text)
        self.assertIn("id=history-action class=seg role=radiogroup", launcher.text)
        self.assertIn('aria-label="Settings"', launcher.text)
        self.assertIn("Ask again", launcher.text)
        response = self.client.get(
            "/view", params={"src": str(self.document), "folder": str(self.root)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("askw-doc-token", response.text)
        library = self.client.get("/api/library").json()
        self.assertEqual(library["documents"][0]["title"], "Guide")

        updated = self.client.post(
            "/api/settings",
            json={
                "token": self.config.token,
                "settings": {
                    "model": "opus",
                    "response_style": "balanced",
                    "glass_transparency": 52,
                    "appearance_theme": "dark",
                },
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["settings"]["model"], "opus")
        self.assertEqual(updated.json()["settings"]["glass_transparency"], 52)
        self.assertEqual(updated.json()["settings"]["appearance_theme"], "dark")
        self.assertEqual(self.client.get("/config").json()["appearance_theme"], "dark")
        dark_launcher = self.client.get("/")
        self.assertIn('<html data-theme="dark">', dark_launcher.text)
        self.assertIn('name=appearance_theme', dark_launcher.text)

        provider_update = self.client.post(
            "/api/settings",
            json={
                "token": self.config.token,
                "settings": {
                    "provider": "codex",
                    "codex_model": "gpt-5.6-terra",
                    "codex_effort": "high",
                },
            },
        )
        self.assertEqual(provider_update.status_code, 200)
        selected = provider_update.json()["settings"]
        self.assertEqual(selected["provider"], "codex")
        self.assertEqual(selected["model"], "gpt-5.6-terra")
        self.assertEqual(selected["reasoning_effort"], "high")

    def test_local_html_interactions_are_enabled(self) -> None:
        document = self.root / "interactive.html"
        document.write_text(
            "<html><body><button onclick=\"document.body.dataset.clicked='yes'\">Run</button>"
            "<script>document.body.dataset.ready='yes'</script></body></html>",
            encoding="utf-8",
        )

        response = self.client.get("/view", params={"src": str(document)})

        self.assertEqual(response.status_code, 200)
        self.assertIn("onclick=", response.text)
        self.assertIn("dataset.ready", response.text)
        self.assertIn(
            "script-src 'self' 'unsafe-inline'",
            response.headers["content-security-policy"],
        )

    def test_model_catalog_reports_subscription_providers(self) -> None:
        catalogs = [
            {
                "id": "claude",
                "label": "Claude",
                "subscription": True,
                "models": [{"id": "sonnet", "label": "Claude Sonnet"}],
            },
            {
                "id": "codex",
                "label": "Codex",
                "subscription": True,
                "models": [{"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol"}],
            },
        ]
        with patch("onyx.app.provider_catalogs", return_value=catalogs):
            response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["providers"]], ["claude", "codex"])

    def test_web_lookups_setting_reaches_the_provider_run(self) -> None:
        seen: dict = {}

        async def fake_stream(*args, **kwargs):
            seen["append_system"] = args[4]
            seen["web"] = kwargs.get("web")
            yield _sse("token", {"text": "Fine."})
            yield _sse("done", {"elapsed_ms": 1})

        body = {
            "token": self.config.token,
            "action": "eli5",
            "selection": "The server is local.",
            "folder": str(self.root),
        }
        self.assertIs(self.client.get("/api/settings").json()["settings"]["web_lookups"], True)
        for enabled in (False, True):
            with self.subTest(web_lookups=enabled):
                saved = self.client.post(
                    "/api/settings", json={"token": self.config.token, "settings": {"web_lookups": enabled}}
                )
                self.assertIs(saved.json()["settings"]["web_lookups"], enabled)
                with patch("onyx.app.stream_answer", fake_stream):
                    self.client.post("/ask", json=body)
                self.assertIs(seen["web"], enabled)
                self.assertEqual("search the web" in seen["append_system"], enabled)

    def test_page_question_without_selection_uses_page_context_and_saves_history(self) -> None:
        prompts = []

        async def fake_stream(*args, **kwargs):
            prompts.append(args[1])
            yield _sse("token", {"text": "It describes the local server."})
            yield _sse("done", {"elapsed_ms": 1})

        body = {
            "token": self.config.token,
            "action": "ask",
            "selection": "",
            "context": "Guide: The server is local.",
            "question": "What is this page about?",
            "document_source": str(self.document),
            "document_title": "Guide",
            "folder": str(self.root),
        }
        with patch("onyx.app.stream_answer", fake_stream):
            response = self.client.post("/ask", json=body)
        self.assertIn("event: done", response.text)
        self.assertIn("Page text (excerpt):", prompts[0])
        self.assertIn("Question about this document: What is this page about?", prompts[0])
        self.assertNotIn("Highlighted passage:", prompts[0])

        page_asks = self.client.get("/api/history", params={"source": str(self.document), "page_only": 1})
        self.assertEqual(len(page_asks.json()["conversations"]), 1)
        self.assertEqual(page_asks.json()["conversations"][0]["selection"], "")

        rejected = self.client.post("/ask", json={**body, "action": "prove"})
        self.assertIn("No text was selected", rejected.text)
        rejected = self.client.post("/ask", json={**body, "document_source": ""})
        self.assertIn("No text was selected", rejected.text)

    def test_stream_is_traced_and_saved(self) -> None:
        async def fake_stream(*args, **kwargs):
            yield _sse("tool_trace", {"tool": "Read", "input": {"file_path": "src/app.py"}})
            yield _sse("token", {"text": "Supported by `guide.md:3`."})
            yield _sse(
                "citations",
                {"items": [{"kind": "file", "path": str(self.document), "line": 3}]},
            )
            yield _sse("done", {"elapsed_ms": 12})

        body = {
            "token": self.config.token,
            "action": "prove",
            "selection": "The server is local.",
            "context": "Guide context",
            "folder": str(self.root),
            "document_source": str(self.document),
            "document_title": "Guide",
            "request_mode": "rerun",
            "parent_request_id": "parent-request",
        }
        with patch("onyx.app.stream_answer", fake_stream):
            response = self.client.post("/ask", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: meta", response.text)
        self.assertIn("event: tool_trace", response.text)
        self.assertIn("event: citations", response.text)
        history = self.client.get(
            "/api/history", params={"source": str(self.document), "selection": body["selection"]}
        ).json()["conversations"]
        self.assertEqual(history[0]["status"], "complete")
        self.assertEqual(history[0]["request_mode"], "rerun")
        self.assertEqual(history[0]["parent_request_id"], "parent-request")
        self.assertEqual(history[0]["effort"], "medium")
        self.assertEqual(history[0]["answer"], "Supported by `guide.md:3`.")
        self.assertEqual(history[0]["trace"][0]["tool"], "Read")
        detail = self.client.get(f"/api/conversations/{history[0]['request_id']}").json()
        self.assertEqual(detail["conversation"]["question"], "")
        filtered = self.client.get("/api/library", params={"provider": "claude"}).json()
        self.assertEqual(len(filtered["conversations"]), 1)
        self.assertIn("providers", filtered["facets"])
        self.assertEqual(
            self.client.get("/api/library", params={"provider": "codex"}).json()["conversations"],
            [],
        )

    def test_mutations_require_the_server_token(self) -> None:
        response = self.client.post("/api/settings", json={"settings": {"model": "opus"}})
        self.assertEqual(response.status_code, 403)

    def test_host_origin_and_document_capability_boundaries(self) -> None:
        bad_host = self.client.get(
            "/view",
            params={"src": str(self.document)},
            headers={"host": "attacker.example"},
        )
        self.assertEqual(bad_host.status_code, 403)

        bad_origin = self.client.get(
            "/api/library", headers={"origin": "https://attacker.example"}
        )
        self.assertEqual(bad_origin.status_code, 403)

        viewed = self.client.get("/view", params={"src": str(self.document)})
        match = re.search(r'name="askw-doc-token" content="([^"]+)"', viewed.text)
        self.assertIsNotNone(match)
        capability = match.group(1)
        current = self.client.get(
            "/_mtime", params={"src": str(self.document), "cap": capability}
        )
        self.assertTrue(current.json()["ok"])
        self.app.state.asset_caps[capability]["expires"] = 0
        expired = self.client.get(
            "/_mtime", params={"src": str(self.document), "cap": capability}
        )
        self.assertFalse(expired.json()["ok"])

    def page_capability(self, path: Path, **params: str) -> str:
        page = self.client.get("/view", params={"src": str(path), **params})
        return re.search(r'name="askw-doc-token" content="([^"]+)"', page.text).group(1)

    def test_editing_saves_only_the_note_its_page_opened_and_never_over_a_newer_version(self) -> None:
        src, cap = str(self.document), self.page_capability(self.document)
        other = self.root / "other.md"
        other.write_text("# Other\n", encoding="utf-8")
        # The source comes only with the page's own capability, for the page's own file.
        self.assertEqual(self.client.get("/api/source", params={"src": src, "cap": "forged"}).status_code, 403)
        self.assertEqual(
            self.client.get("/api/source", params={"src": str(other.resolve()), "cap": cap}).status_code, 403
        )
        opened = self.client.get("/api/source", params={"src": src, "cap": cap}).json()
        self.assertEqual(opened["text"], "# Guide\n\nThe server is local.")

        save = {"token": self.config.token, "edit": opened["edit"], "base": opened["sig"], "text": "# Guide\n\nEdited."}
        self.assertEqual(self.client.post("/api/source", json={**save, "token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/api/source", json={**save, "edit": "forged"}).status_code, 403)
        self.assertEqual(
            self.client.post("/api/source", json=save, headers={"Origin": "https://attacker.example"}).status_code, 403
        )
        self.assertEqual(self.document.read_text(encoding="utf-8"), "# Guide\n\nThe server is local.")
        # A path in the request names nothing: the edit capability is the file.
        saved = self.client.post("/api/source", json={**save, "src": str(other)}).json()
        self.assertTrue(saved["ok"])
        self.assertEqual(self.document.read_text(encoding="utf-8"), "# Guide\n\nEdited.")
        self.assertEqual(other.read_text(encoding="utf-8"), "# Other\n")
        # Live reload sees the version the save returned, so the page doesn't take its own save for someone else's.
        self.assertEqual(self.client.get("/_mtime", params={"src": src, "cap": cap}).json()["sig"], saved["sig"])

        # Saved against the version before: refused, with the one on disk, and nothing overwritten.
        stale = self.client.post("/api/source", json={**save, "text": "clobber"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["sig"], saved["sig"])
        self.assertEqual(self.document.read_text(encoding="utf-8"), "# Guide\n\nEdited.")

        # Pages that aren't Markdown notes aren't editable.
        for name, body in (("page.html", "<p>Authored</p>"), ("plain.txt", "Plain")):
            path = self.root / name
            path.write_text(body, encoding="utf-8")
            refused = self.client.get("/api/source", params={"src": str(path.resolve()), "cap": self.page_capability(path)})
            self.assertEqual(refused.status_code, 400)

    def test_a_task_box_on_the_page_ticks_its_line_in_the_note(self) -> None:
        note = self.root / "tasks.md"
        note.write_text("# Tasks\n\n- [ ] write it\n- [ ] ship it\n", encoding="utf-8")
        src, cap = str(note.resolve()), self.page_capability(note)
        base = self.client.get("/_mtime", params={"src": src, "cap": cap}).json()["sig"]
        tick = {"token": self.config.token, "src": src, "cap": cap, "line": 3, "done": True, "base": base}
        self.assertEqual(self.client.post("/api/source/task", json={**tick, "token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/api/source/task", json={**tick, "cap": "forged"}).status_code, 403)
        ticked = self.client.post("/api/source/task", json=tick).json()
        self.assertTrue(ticked["ok"])
        self.assertEqual(note.read_text(encoding="utf-8"), "# Tasks\n\n- [ ] write it\n- [x] ship it\n")
        # Against the version the page was showing: once the note has moved on, refused.
        self.assertEqual(self.client.post("/api/source/task", json={**tick, "line": 2}).status_code, 409)
        self.assertEqual(self.client.post("/api/source/task", json={**tick, "line": 0, "base": ticked["sig"]}).status_code, 400)

    def test_a_tick_is_made_against_the_version_the_page_shows(self) -> None:
        # The page names the version it rendered; live reload may since have seen a newer one while a reload waits
        # (an answer open). A tick against the page's version is refused once the note has moved on, so line 2 of
        # the old text never ticks line 2 of the new.
        note = self.root / "shown.md"
        note.write_text("- [ ] first\n- [ ] second\n", encoding="utf-8")
        page = self.client.get("/view", params={"src": str(note)}).text
        shown = re.search(r'name="askw-doc-sig" content="([^"]+)"', page).group(1)
        cap = re.search(r'name="askw-doc-token" content="([^"]+)"', page).group(1)
        src = str(note.resolve())
        self.assertEqual(shown, self.client.get("/_mtime", params={"src": src, "cap": cap}).json()["sig"])
        note.write_text("- [ ] prepended\n- [ ] first\n- [ ] second\n", encoding="utf-8")
        tick = {"token": self.config.token, "src": src, "cap": cap, "line": 0, "done": True, "base": shown}
        self.assertEqual(self.client.post("/api/source/task", json=tick).status_code, 409)
        self.assertEqual(note.read_text(encoding="utf-8"), "- [ ] prepended\n- [ ] first\n- [ ] second\n")

    def test_the_editor_reads_the_note_again_without_minting_capabilities(self) -> None:
        src, cap = str(self.document), self.page_capability(self.document)
        opened = self.client.get("/api/source", params={"src": src, "cap": cap}).json()
        minted = len(self.app.state.edit_caps)
        self.document.write_text("# Guide\n\nChanged elsewhere.", encoding="utf-8")
        for _ in range(3):
            now = self.client.get("/api/source/current", params={"edit": opened["edit"]}).json()
        self.assertEqual(now["text"], "# Guide\n\nChanged elsewhere.")
        self.assertEqual(len(self.app.state.edit_caps), minted)
        self.assertEqual(self.client.get("/api/source/current", params={"edit": "forged"}).status_code, 403)
        # One in use stays while more notes are opened for editing than the server keeps.
        for i in range(80):
            other = self.root / f"other-{i}.md"
            other.write_text(f"# {i}\n", encoding="utf-8")
            self.client.get("/api/source", params={"src": str(other.resolve()), "cap": self.page_capability(other)})
            if i % 20 == 0:
                self.client.get("/api/source/current", params={"edit": opened["edit"]})
        saved = self.client.post("/api/source", json={
            "token": self.config.token, "edit": opened["edit"], "base": now["sig"], "text": "# Guide\n\nStill mine.",
        })
        self.assertEqual(saved.status_code, 200)

    def test_the_editor_draws_only_images_the_saved_note_references(self) -> None:
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.root / "other.png").write_bytes(b"\x89PNG\r\n\x1a\nother")
        (self.root / "doc.pdf").write_bytes(b"%PDF-1.4")
        note = self.root / "pictures.md"
        note.write_text("# Pictures\n\n![a picture](pic.png) ![](doc.pdf) ![](https://example.com/x.png)\n", encoding="utf-8")
        src, cap = str(note.resolve()), self.page_capability(note)
        opened = self.client.get("/api/source", params={"src": src, "cap": cap}).json()
        ask = {"token": self.config.token, "edit": opened["edit"], "refs": [
            {"kind": "md", "target": "pic.png"}, {"kind": "md", "target": "other.png"},
            {"kind": "md", "target": "doc.pdf"}, {"kind": "md", "target": "https://example.com/x.png"},
        ]}
        self.assertEqual(self.client.post("/api/source/images", json={**ask, "token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/api/source/images", json={**ask, "edit": "forged"}).status_code, 403)
        urls = self.client.post("/api/source/images", json=ask).json()["urls"]
        home = patch("pathlib.Path.home", return_value=self.root.resolve())  # /_fs serves only under home
        home.start()
        self.addCleanup(home.stop)
        self.assertEqual(urls["md:https://example.com/x.png"], "https://example.com/x.png")
        self.assertIsNone(urls["md:doc.pdf"])      # not an image
        self.assertIsNone(urls["md:other.png"])    # an image, but not one the note references
        self.assertEqual(self.client.get(urls["md:pic.png"]).content, b"\x89PNG\r\n\x1a\nfake")
        stranger = urls["md:pic.png"].replace(urllib.parse.quote(str((self.root / "pic.png").resolve())),
                                              urllib.parse.quote(str((self.root / "other.png").resolve())))
        self.assertEqual(self.client.get(stranger).status_code, 404)
        # Once the note is saved referencing it, it is drawn too.
        self.client.post("/api/source", json={"token": self.config.token, "edit": opened["edit"], "base": opened["sig"],
                                              "text": note.read_text(encoding="utf-8") + "\n![](other.png)\n"})
        urls = self.client.post("/api/source/images", json=ask).json()["urls"]
        self.assertEqual(self.client.get(urls["md:other.png"]).content, b"\x89PNG\r\n\x1a\nother")

    def test_position_and_export_round_trip(self) -> None:
        self.client.get("/view", params={"src": str(self.document)})
        updated = self.client.post(
            "/api/position",
            json={
                "token": self.config.token,
                "source": str(self.document),
                "scroll_y": 412.5,
            },
        )
        self.assertEqual(updated.status_code, 200)
        document = self.client.get(
            "/api/document", params={"source": str(self.document)}
        ).json()["document"]
        self.assertEqual(document["scroll_y"], 412.5)
        exported = self.client.get("/api/export", params={"src": str(self.document)})
        self.assertEqual(exported.status_code, 200)
        self.assertIn("Guide", exported.text)


class VaultApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "context"
        self.root.mkdir()
        self.vault, self.outside = make_vault(self.base)
        self.config = AppConfig(
            default_folder=self.root,
            allowed_roots=(self.root,),
            port=8899,
            data_dir=self.base / "data",
        )
        self.app = create_app(self.config)
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def set_vault(self, value: str):
        return self.client.post(
            "/api/settings", json={"token": self.config.token, "settings": {"vault_root": value}}
        )

    def test_saving_vault_root_registers_an_allowed_root(self) -> None:
        self.assertEqual(self.set_vault(str(self.vault)).status_code, 200)
        roots = [item["path"] for item in self.client.get("/api/settings").json()["roots"]]
        self.assertIn(str(self.vault.resolve()), roots)
        self.assertEqual(self.client.get("/api/settings").json()["settings"]["vault_root"], str(self.vault))
        invalid = self.set_vault(str(self.base / "missing"))
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("does not exist", invalid.json()["error"])

    def test_tree_and_search_are_scoped_and_guarded(self) -> None:
        self.set_vault(str(self.vault))
        tree = self.client.get("/api/vault/tree").json()
        self.assertTrue(tree["ok"])
        self.assertEqual(tree["root"], str(self.vault))
        self.assertEqual(tree["files"], 4)
        names = [child["name"] for child in tree["tree"]["children"]]
        self.assertEqual(names, ["linked", "notes", "Beta.md"])
        self.assertNotIn(".obsidian", names)
        linked = next(child for child in tree["tree"]["children"] if child["name"] == "linked")
        self.assertTrue(linked["symlink"])
        self.assertEqual(linked["children"][0]["path"], str(self.vault / "linked" / "L.md"))
        self.assertNotIn(str(self.outside), str(tree))

        search = self.client.get("/api/vault/search", params={"q": "al"}).json()
        self.assertEqual([item["name"] for item in search["items"]], ["Alpha.md"])
        self.assertEqual(search["items"][0]["folder"], "notes")
        self.assertEqual(self.client.get("/api/vault/search", params={"q": ""}).json()["items"], [])

        forbidden = self.client.get("/api/vault/tree", headers={"origin": "https://attacker.example"})
        self.assertEqual(forbidden.status_code, 403)

        self.set_vault("")
        unset = self.client.get("/api/vault/tree")
        self.assertEqual(unset.status_code, 400)
        self.assertEqual(unset.json()["error"], "No vault folder is configured.")
        self.assertEqual(self.client.get("/api/vault/search", params={"q": "al"}).status_code, 400)

    def test_saved_answers_are_drawn_with_the_widgets_own_markdown(self) -> None:
        # Recent conversations lifts ask.js's renderer out by its section rules; lose one and every answer reads raw.
        renderer = answer_markdown()
        self.assertTrue(renderer.startswith("(()=>{"), renderer[:80])
        self.assertIn("function mdToHtml(src)", renderer)
        self.assertNotIn("</script", renderer.lower())  # it is inlined in the shell's <script>
        page = self.client.get("/").text
        self.assertIn("answerHtml=(()=>{", page)
        self.assertNotIn("__ANSWER_MARKDOWN__", page)

    def test_vault_page_embeds_the_reader_iframe(self) -> None:
        self.set_vault(str(self.vault))
        note = self.vault / "notes" / "Alpha.md"
        page = self.client.get("/vault", params={"src": str(note), "history": "abc", "history_action": "rerun"})
        self.assertEqual(page.status_code, 200)
        expected = urllib.parse.urlencode(
            {"src": str(note), "folder": str(self.vault), "history": "abc", "history_action": "rerun"},
            quote_via=urllib.parse.quote,
            safe="/",
        )
        self.assertIn(f'<iframe id=reader name=reader src="/view?{expected.replace("&", "&amp;")}"', page.text)
        self.assertIn("<title>Alpha.md — Vault</title>", page.text)
        # The outline pane and its toggle ship with every view; the shell fills the pane from the reader's headings.
        self.assertIn("<aside id=outline-side", page.text)
        self.assertIn("id=outline-toggle", page.text)
        self.assertIn("<title>Vault</title>", self.client.get("/vault").text)  # no page: the name once
        self.assertIn('<a href="/vault" data-kind=notes>Notes</a>', self.client.get("/").text)
        self.assertIn("id=vault-form", self.client.get("/").text)
        blank = self.client.get("/vault")
        self.assertIn('src="about:blank"', blank.text)
        self.assertEqual(
            self.client.get("/vault", headers={"host": "attacker.example"}).status_code, 403
        )

    def test_symlinked_vault_note_keeps_vault_visible_links(self) -> None:
        self.set_vault(str(self.vault))
        note = self.vault / "linked" / "L.md"
        page = self.client.get("/view", params={"src": str(note), "folder": str(self.vault)})
        self.assertEqual(page.status_code, 200)
        sibling = urllib.parse.quote(str(self.vault / "linked" / "M.md"))
        # The folder seed is the resolved context folder (as ask.js expects);
        # the link target stays the vault-visible path through the symlink.
        self.assertIn(
            f'href="/view?src={sibling}&amp;folder={urllib.parse.quote(str(self.vault.resolve()))}"', page.text
        )
        self.assertIn(
            f'<meta name="askw-src" content="{(self.outside / "L.md").resolve()}">', page.text
        )
        self.assertNotIn(str(self.outside.resolve() / "M.md"), page.text)

        alpha = self.client.get("/view", params={"src": str(self.vault / "notes" / "Alpha.md")})
        self.assertIn('class="askw-properties"', alpha.text)
        self.assertIn('class="askw-wikilink-missing"', alpha.text)
        self.assertIn(urllib.parse.quote(str(self.vault / "Beta.md")), alpha.text)

        outside_note = self.client.get("/view", params={"src": str(self.outside / "L.md")})
        self.assertIn("[[M]]", outside_note.text)  # outside the vault: literal wikilink

    def test_a_note_edited_through_a_linked_folder_saves_to_its_real_file_and_follows_its_links(self) -> None:
        self.set_vault(str(self.vault))
        note = self.vault / "linked" / "L.md"
        page = self.client.get("/view", params={"src": str(note), "folder": str(self.vault)})
        cap = re.search(r'name="askw-doc-token" content="([^"]+)"', page.text).group(1)
        real = str((self.outside / "L.md").resolve())
        opened = self.client.get("/api/source", params={"src": real, "cap": cap}).json()
        self.assertEqual(opened["text"], "# Linked\n\n[[M]]\n")

        # A link clicked in the editor leads where the same link on the page does: from the note's vault path.
        def follow(target: str, kind: str) -> dict:
            return self.client.get("/api/source/link", params={"edit": opened["edit"], "target": target, "kind": kind}).json()

        sibling = "/view?src=" + urllib.parse.quote(str(self.vault / "linked" / "M.md"))
        folder = "&folder=" + urllib.parse.quote(str(self.vault.resolve()))
        self.assertEqual(follow("M", "wiki")["href"], sibling + folder)
        self.assertEqual(follow("M#Part", "wiki")["href"], sibling + folder + "#Part")
        self.assertEqual(follow("M.md", "md")["href"], sibling + folder)
        self.assertFalse(follow("Nowhere", "wiki")["ok"])
        self.assertFalse(follow("https://example.com", "md")["ok"])  # the editor opens those itself
        self.assertEqual(
            self.client.get("/api/source/link", params={"edit": "forged", "target": "M", "kind": "wiki"}).status_code, 403
        )

        saved = self.client.post("/api/source", json={
            "token": self.config.token, "edit": opened["edit"], "base": opened["sig"], "text": "# Linked\n\n[[M]] again\n",
        }).json()
        self.assertTrue(saved["ok"])
        self.assertTrue((self.vault / "linked").is_symlink())
        self.assertEqual((self.outside / "L.md").read_text(encoding="utf-8"), "# Linked\n\n[[M]] again\n")

        # An embedded image resolves as the page's does, by the vault's rules, once the note references it.
        (self.vault / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nshot")
        self.app.state.vault.invalidate()  # a new file, inside the index's few seconds of caching
        self.client.post("/api/source", json={
            "token": self.config.token, "edit": opened["edit"], "base": saved["sig"], "text": "# Linked\n\n![[shot.png]]\n",
        })
        urls = self.client.post("/api/source/images", json={
            "token": self.config.token, "edit": opened["edit"], "refs": [{"kind": "wiki", "target": "shot.png"}],
        }).json()["urls"]
        with patch("pathlib.Path.home", return_value=self.base.resolve()):
            self.assertEqual(self.client.get(urls["wiki:shot.png"]).content, b"\x89PNG\r\n\x1a\nshot")

    def test_history_says_which_vault_a_document_lives_in_and_its_row_there(self) -> None:
        # History keys a note by its real file; the shell lists it through the link, and says where.
        self.set_vault(str(self.vault))
        self.client.get("/view", params={"src": str(self.vault / "linked" / "L.md"), "folder": str(self.vault)})
        doc = self.client.get("/api/library").json()["documents"][0]
        self.assertEqual(doc["source"], str((self.outside / "L.md").resolve()))
        self.assertEqual(
            (doc["vault"], doc["vault_path"], doc["vault_folder"]), ("notes", str(self.vault / "linked" / "L.md"), "linked")
        )

    def test_row_menu_entry_reads_the_notes_vault_by_default(self) -> None:
        self.set_vault(str(self.vault))
        linked = self.client.get("/api/vault/entry", params={"path": str(self.vault / "linked" / "L.md")}).json()
        self.assertEqual((linked["real"], linked["link"]), (str((self.outside / "L.md").resolve()), str(self.vault / "linked")))
        own = self.client.get("/api/vault/entry", params={"path": str(self.vault / "Beta.md")}).json()
        self.assertEqual((own["real"], own["link"], own["is_dir"]), (str(self.vault / "Beta.md"), None, False))

    def test_startup_registers_the_configured_vault_root(self) -> None:
        data = self.base / "seeded"
        store = Storage(data)
        store.update_settings({"vault_root": str(self.vault)}, model_default="sonnet")
        store.close()
        app = create_app(
            AppConfig(default_folder=self.root, allowed_roots=(self.root,), port=8899, data_dir=data)
        )
        roots = [item["path"] for item in app.state.storage.roots()]
        self.assertIn(str(self.vault.resolve()), roots)
        app.state.storage.close()


class HtmlVaultApiTests(unittest.TestCase):
    """Artifacts: a folder of symlinks, browsed and extended from the shell."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.context = self.base / "context"
        self.context.mkdir()
        self.vault = self.base / "Artifacts"
        self.topic = self.base / "learnings" / "topics" / "architect"
        self.guide = self.topic / "guides" / "who-holds-the-plan"
        (self.guide / "audio").mkdir(parents=True)
        (self.vault / "Mine").mkdir(parents=True)
        (self.guide / "index.html").write_text(
            '<html><head><title>Who holds the plan</title></head><body><section id="predict">'
            '<p>Before you read.</p><audio controls preload="none" src="audio/one.m4a"></audio>'
            "</section></body></html>",
            encoding="utf-8",
        )
        self.audio = self.guide / "audio" / "one.m4a"
        self.audio.write_bytes(bytes(range(256)) * 4)  # 1024 bytes
        (self.vault / "Architect").symlink_to(self.topic, target_is_directory=True)
        self.page = self.vault / "Architect" / "guides" / "who-holds-the-plan" / "index.html"
        self.config = AppConfig(
            default_folder=self.context,
            allowed_roots=(self.context,),
            port=8899,
            data_dir=self.base / "data",
        )
        self.app = create_app(self.config)
        self.app.state.storage.update_settings({"html_vault_root": str(self.vault)}, model_default="sonnet")
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def post(self, url: str, body: dict, token: bool = True):
        return self.client.post(url, json={**({"token": self.config.token} if token else {}), **body})

    def test_dock_recents_include_only_viewed_artifacts_and_notes(self) -> None:
        notes = self.base / "Notes"
        notes.mkdir()
        note = notes / "Field notes.md"
        note.write_text("# Field notes\n", encoding="utf-8")
        self.assertEqual(self.post("/api/settings", {"settings": {"vault_root": str(notes)}}).status_code, 200)
        loose = self.context / "loose.md"
        loose.write_text("# Loose\n", encoding="utf-8")
        for source, title, kind in (
            (self.guide / "index.html", "Who holds the plan", "html"),
            (note, "Field notes", "markdown"),
            (loose, "Loose", "markdown"),
            (notes / "gone.md", "Gone", "markdown"),
        ):
            self.app.state.storage.upsert_document(
                source=str(source), title=title, kind=kind, folder=str(source.parent)
            )
        response = self.client.get("/api/dock/recent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["artifacts"], [{
            "title": "Who holds the plan", "path": str(self.guide / "index.html"),
            "folder": "Architect/guides",
        }])
        self.assertEqual(response.json()["notes"], [{
            "title": "Field notes", "path": str(note), "folder": "",
        }])
        self.assertEqual(self.client.get("/api/dock/recent", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_remove_from_recents_hides_and_never_deletes(self) -> None:
        storage = self.app.state.storage
        page = str(self.guide / "index.html")
        doc_id = storage.upsert_document(source=page, title="Who holds the plan", kind="html", folder=str(self.base))
        storage.update_position(page, 420)
        for rid, parent in (("req-1", None), ("req-2", "req-1")):
            storage.start_conversation(
                request_id=rid, document_id=doc_id, document_source=page, document_title="Who holds the plan",
                document_page=None, selection="Before you read.", context="", action="ask", question=rid,
                folder=str(self.base), provider="claude", model="sonnet", parent_request_id=parent,
            )
            storage.finish_conversation(rid, status="complete", answer="Yes.")

        self.assertEqual(self.post("/api/recent/remove", {"source": page}, token=False).status_code, 403)
        self.assertEqual(self.post("/api/recent/remove", {}).status_code, 400)
        self.assertEqual(self.post("/api/recent/remove", {"source": "/nowhere.md"}).status_code, 404)

        self.assertEqual(self.post("/api/recent/remove", {"source": page}).status_code, 200)
        self.assertEqual(self.client.get("/api/library").json()["documents"], [])
        self.assertEqual(self.client.get("/api/dock/recent").json()["artifacts"], [])
        self.assertEqual(storage.document(page)["scroll_y"], 420)  # hidden, not deleted: the place is kept
        storage.upsert_document(source=page, title="Who holds the plan", kind="html", folder=str(self.base))
        self.assertEqual([d["source"] for d in self.client.get("/api/library").json()["documents"]], [page])

        # A thread goes by its latest turn, and every turn it followed goes with it; History still has them all.
        self.assertEqual(self.post("/api/recent/remove", {"request_id": "req-2"}).status_code, 200)
        asks = {c["request_id"]: c["recent_hidden"] for c in self.client.get("/api/library").json()["conversations"]}
        self.assertEqual(asks, {"req-1": 1, "req-2": 1})
        storage.start_conversation(
            request_id="req-3", document_id=doc_id, document_source=page, document_title="Who holds the plan",
            document_page=None, selection="Before you read.", context="", action="ask", question="again",
            folder=str(self.base), provider="claude", model="sonnet", parent_request_id="req-2",
        )
        storage.finish_conversation("req-3", status="complete", answer="Still yes.")
        latest = self.client.get("/api/library").json()["conversations"][0]
        self.assertEqual((latest["request_id"], latest["recent_hidden"]), ("req-3", 0))  # asking on brings it back

    def test_tree_search_and_shell_use_titles_and_the_html_kind(self) -> None:
        tree = self.client.get("/api/vault/tree", params={"vault": "html"}).json()
        self.assertTrue(tree["ok"])
        self.assertEqual(tree["vault"], "html")
        self.assertEqual(tree["files"], 1)
        architect = next(c for c in tree["tree"]["children"] if c["name"] == "Architect")
        page = architect["children"][0]["children"][0]
        self.assertEqual((page["title"], page["path"]), ("Who holds the plan", str(self.page)))
        self.assertIn("Mine", [c["name"] for c in tree["tree"]["children"]])
        search = self.client.get("/api/vault/search", params={"vault": "html", "q": "plan"}).json()
        self.assertEqual([(i["title"], i["folder"]) for i in search["items"]], [("Who holds the plan", "Architect/guides")])

        shell = self.client.get("/vault", params={"vault": "html", "src": str(self.page)})
        self.assertEqual(shell.status_code, 200)
        expected = urllib.parse.urlencode({"src": str(self.page)}, quote_via=urllib.parse.quote, safe="/")
        self.assertIn(f'<iframe id=reader name=reader src="/view?{expected}"', shell.text)  # no vault folder
        self.assertIn("<title>index.html — Artifacts</title>", shell.text)
        self.assertIn("<title>Artifacts</title>", self.client.get("/vault", params={"vault": "html"}).text)
        self.assertIn('let KIND="html"', shell.text)
        self.assertIn("id=add-panel", shell.text)
        self.assertIn('<a href="/vault?vault=html" class=active', shell.text)
        # One shell for both vaults, so a switch needs no reload: Notes carries the + panel too, shown only in Artifacts.
        notes = self.client.get("/vault").text
        self.assertIn('<body class="kind-notes', notes)
        self.assertIn("body:not(.kind-html) #add-toggle", notes)
        self.assertIn('<a href="/vault?vault=html" data-kind=html>Artifacts</a>', self.client.get("/").text)

        self.app.state.storage.update_settings({"html_vault_root": ""}, model_default="sonnet")
        unset = self.client.get("/api/vault/tree", params={"vault": "html"})
        self.assertEqual(unset.json()["error"], "No Artifacts folder is configured.")

    def test_library_opens_a_real_file_as_its_row_and_a_loose_one_with_its_folder(self) -> None:
        real = self.guide / "index.html"
        self.assertEqual(self.client.get("/view", params={"src": str(self.page)}).status_code, 200)
        doc = self.client.get("/api/library").json()["documents"][0]
        self.assertEqual(doc["source"], str(real))
        self.assertEqual((doc["vault"], doc["vault_path"], doc["vault_folder"]), ("html", str(self.page), "Architect/guides"))
        # Finder hands over the real file; Library reads it as the page in Artifacts, with the link's context.
        shell = self.client.get("/", params={"src": str(real)})
        expected = urllib.parse.urlencode({"src": str(self.page)}, quote_via=urllib.parse.quote, safe="/")
        self.assertIn(f'<iframe id=reader name=reader src="/view?{expected}"', shell.text)
        self.assertIn('<body class="kind-library', shell.text)
        self.assertIn("<title>index.html — Library</title>", shell.text)
        self.assertIn("<section id=home data-drag aria-label=\"Library\" hidden>", shell.text)  # a page is open: no home page
        # A document in neither vault keeps the folder it came with.
        loose = self.context / "loose.md"
        loose.write_text("# Loose\n", encoding="utf-8")
        page = self.client.get("/", params={"src": str(loose), "folder": str(self.context)})
        expected = urllib.parse.urlencode({"src": str(loose), "folder": str(self.context)}, quote_via=urllib.parse.quote, safe="/")
        self.assertIn(f'src="/view?{expected.replace("&", "&amp;")}"', page.text)
        self.assertIn("<section id=home data-drag aria-label=\"Library\">", self.client.get("/").text)  # nothing open: home

    def test_evidence_opens_in_the_reader_as_its_row_at_the_cited_passage(self) -> None:
        # An answer cites the real file; the reader opens it as its row in Artifacts, at the words on the cited line.
        real = self.guide / "index.html"
        real.write_text(
            "<html><head><title>Who holds the plan</title></head><body>\n"
            '<section id="predict">\n'
            "<p>Before you read.</p>\n"
            "</section>\n"
            '<details id="sources"><summary>Claims</summary><table>\n'
            "<tr><td>wh-01</td><td>The harness holds the plan &amp; the model reads it.</td></tr>\n"
            "</table></details></body></html>\n",
            encoding="utf-8",
        )
        self.assertEqual(self.client.get("/view", params={"src": str(self.page)}).status_code, 200)  # a known document
        note = self.context / "notes.md"
        note.write_text("# Notes\n\n- The **plan** lives with the [harness](https://example.com), not the model.\n", encoding="utf-8")
        code = self.context / "harness.py"
        code.write_text("plan = []\n", encoding="utf-8")
        with patch("onyx.app.open_source") as opened:
            reply = self.post("/api/open-source", {"path": str(real), "line": 6, "reader": True}).json()
            expected = urllib.parse.urlencode({"src": str(self.page)}, quote_via=urllib.parse.quote, safe="/")
            self.assertEqual(reply["view"], f"/view?{expected}")  # the link's own context, as the sidebar opens it
            self.assertEqual((reply["text"], reply["anchor"]), ("wh-01 The harness holds the plan & the model reads it.", "sources"))

            # A note in neither vault reads in the answer's context.
            reply = self.post("/api/open-source", {"path": str(note), "line": 3, "folder": str(self.context), "reader": True}).json()
            expected = urllib.parse.urlencode({"src": str(note), "folder": str(self.context)}, quote_via=urllib.parse.quote, safe="/")
            self.assertEqual(reply["view"], f"/view?{expected}")
            self.assertEqual((reply["text"], reply["anchor"]), ("The plan lives with the harness, not the model.", None))
            opened.assert_not_called()

            # Code has no reader page: it still opens in the editor, and the reply sends the widget nowhere.
            reply = self.post("/api/open-source", {"path": str(code), "line": 1, "folder": str(self.context), "reader": True}).json()
            self.assertEqual(reply, {"ok": True})
            opened.assert_called_once_with(code, line=1, page=None)

            # Without the reader flag (the Obsidian plugin), a page still goes to the editor as before.
            opened.reset_mock()
            self.assertEqual(self.post("/api/open-source", {"path": str(real), "line": 6}).json(), {"ok": True})
            opened.assert_called_once_with(real, line=6, page=None)

    def test_reader_uses_the_real_folder_behind_the_link_when_it_is_allowed(self) -> None:
        # Not yet an allowed root: the reader falls back to the default folder.
        before = self.client.get("/view", params={"src": str(self.page)})
        self.assertIn(f'<meta name="askw-folder" content="{self.context}">', before.text)
        self.client.post("/api/roots", json={"token": self.config.token, "path": str(self.base / "learnings")})
        after = self.client.get("/view", params={"src": str(self.page)})
        self.assertIn(f'<meta name="askw-folder" content="{self.topic}">', after.text)
        # An explicit folder still wins, and the guide's audio is rewritten to a capability URL.
        explicit = self.client.get("/view", params={"src": str(self.page), "folder": str(self.context)})
        self.assertIn(f'<meta name="askw-folder" content="{self.context}">', explicit.text)
        self.assertRegex(after.text, r'<audio controls preload="none" src="http://127\.0\.0\.1:8899/_fs/[^/"]+/')
        self.assertIn(urllib.parse.quote(str(self.audio)), after.text)

    def test_audio_is_served_in_ranges_for_webkit(self) -> None:
        with patch("pathlib.Path.home", return_value=self.base):
            viewed = self.client.get("/view", params={"src": str(self.page)})
            url = re.search(r'<audio controls preload="none" src="http://127\.0\.0\.1:8899([^"]+)"', viewed.text).group(1)
            whole = self.client.get(url)
            self.assertEqual(whole.status_code, 200)
            self.assertEqual(whole.headers["accept-ranges"], "bytes")
            self.assertEqual(whole.headers["content-type"], "audio/mp4")
            self.assertEqual(len(whole.content), 1024)
            probe = self.client.get(url, headers={"range": "bytes=0-1"})
            self.assertEqual(probe.status_code, 206)
            self.assertEqual(probe.headers["content-range"], "bytes 0-1/1024")
            self.assertEqual(probe.content, bytes([0, 1]))
            tail = self.client.get(url, headers={"range": "bytes=-3"})
            self.assertEqual((tail.status_code, tail.content), (206, bytes([253, 254, 255])))
            open_ended = self.client.get(url, headers={"range": "bytes=1000-"})
            self.assertEqual(open_ended.headers["content-range"], "bytes 1000-1023/1024")
            past_end = self.client.get(url, headers={"range": "bytes=5000-"})
            self.assertEqual(past_end.status_code, 416)
            self.assertEqual(past_end.headers["content-range"], "bytes */1024")
            multi = self.client.get(url, headers={"range": "bytes=0-1,4-5"})
            self.assertEqual((multi.status_code, len(multi.content)), (200, 1024))

    def test_link_and_folder_routes_write_only_inside_the_vault(self) -> None:
        other = self.base / "elsewhere" / "report"
        other.mkdir(parents=True)
        (other / "index.html").write_text("<title>Quarterly report</title>", encoding="utf-8")
        self.assertEqual(self.post("/api/vault/html/link", {"target": str(other / "index.html")}, token=False).status_code, 403)
        denied = self.client.post(
            "/api/vault/html/link",
            json={"token": self.config.token, "target": str(other / "index.html")},
            headers={"origin": "https://attacker.example"},
        )
        self.assertEqual(denied.status_code, 403)

        made = self.post("/api/vault/html/folder", {"parent": "", "name": "Reports"})
        self.assertEqual(made.json()["rel"], "Reports")
        linked = self.post("/api/vault/html/link", {"parent": "Reports", "targets": [str(other / "index.html")]}).json()
        self.assertTrue(linked["ok"])
        self.assertEqual(linked["linked"], [str(self.vault / "Reports" / "report.html")])
        self.assertEqual(linked["context_roots"], [str(other)])
        self.assertIn(str(other), [r["path"] for r in self.client.get("/api/settings").json()["roots"]])
        tree = self.client.get("/api/vault/tree", params={"vault": "html"}).json()["tree"]
        reports = next(c for c in tree["children"] if c["name"] == "Reports")
        self.assertEqual(reports["children"][0]["title"], "Quarterly report")

        refused = self.post("/api/vault/html/link", {"parent": "Architect", "target": str(other / "index.html")})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("linked folder", refused.json()["error"])
        self.assertFalse((self.topic / "report.html").exists())
        partial = self.post(
            "/api/vault/html/link",
            {"parent": "Reports", "targets": [str(other / "index.html"), str(self.guide / "index.html")]},
        ).json()
        self.assertTrue(partial["ok"])
        self.assertEqual(len(partial["linked"]), 1)
        self.assertIn("already exists", partial["errors"][0])
        loop = self.post("/api/vault/html/link", {"parent": "Mine", "target": str(self.base)})
        self.assertIn("would loop", loop.json()["error"])
        # A page sitting directly in home links fine, but home never becomes a context root.
        (self.base / "top.html").write_text("<title>Top</title>", encoding="utf-8")
        with patch("pathlib.Path.home", return_value=self.base):
            homed = self.post("/api/vault/html/link", {"parent": "Mine", "target": str(self.base / "top.html")})
        self.assertEqual((homed.json()["ok"], homed.json()["context_roots"]), (True, []))
        self.assertNotIn(str(self.base), [r["path"] for r in self.client.get("/api/settings").json()["roots"]])

    def test_locate_maps_a_real_file_to_its_row_and_refuses_strangers(self) -> None:
        # Finder and Alfred hand over the real file; a tab reads it as the row Artifacts lists it by.
        real = str(self.guide / "index.html")
        found = self.client.get("/api/vault/locate", params={"src": real}).json()
        self.assertEqual((found["ok"], found["vault"], found["path"]), (True, "html", str(self.page)))
        stray = self.base / "context" / "stray.html"
        stray.write_text("<title>Stray</title>", encoding="utf-8")
        loose = self.client.get("/api/vault/locate", params={"src": str(stray)}).json()
        self.assertEqual((loose["vault"], loose["path"]), (None, str(stray)))
        remote = self.client.get("/api/vault/locate", params={"src": "https://example.com/a.html"}).json()
        self.assertEqual((remote["vault"], remote["path"]), (None, "https://example.com/a.html"))
        foreign = self.client.get(
            "/api/vault/locate", params={"src": real}, headers={"origin": "https://attacker.example"}
        )
        self.assertEqual(foreign.status_code, 403)

    def test_the_shell_ships_the_tab_bar_and_its_entry_points(self) -> None:
        shell = self.client.get("/vault", params={"vault": "html", "src": str(self.page)}).text
        for part in ("<div id=tab-bar data-drag>", "<div class=tab-group data-nodrag>", "id=tab-strip role=tablist",
                     "<button id=tab-new", "<button id=tab-list", "<button id=tab-pin", "<div id=stage>"):
            self.assertIn(part, shell)
        # The frame the server draws is still the reader, by name and by id: tab one.
        self.assertIn(f'<iframe id=reader name=reader src="/view?src={urllib.parse.quote(str(self.page))}"', shell)
        # The app's menu reaches the tabs through onyxShell (Onyx.swift, shellCall).
        self.assertIn("...TAB_SHELL}", shell)
        entries = re.search(r"const TAB_SHELL=\{(.*?)\};\n", shell, re.DOTALL)
        self.assertIsNotNone(entries)
        for entry in ("newTab", "closeTab", "nextTab", "prevTab", "openInTab", "openHref", "pinTabs"):
            self.assertIn(f"{entry}:", entries.group(1))

    def test_row_menu_routes_take_a_row_and_work_out_the_rest(self) -> None:
        real = str(self.guide / "index.html")
        entry = self.client.get("/api/vault/entry", params={"vault": "html", "path": str(self.page)}).json()
        self.assertEqual((entry["ok"], entry["real"], entry["link"]), (True, real, str(self.vault / "Architect")))
        # The real file is not a row; only paths the tree lists are.
        self.assertEqual(self.client.get("/api/vault/entry", params={"vault": "html", "path": real}).status_code, 400)
        foreign = self.client.get(
            "/api/vault/entry", params={"vault": "html", "path": str(self.page)}, headers={"origin": "https://attacker.example"}
        )
        self.assertEqual(foreign.status_code, 403)

        with patch("onyx.vault.reveal_in_finder") as reveal:
            self.assertEqual(self.post("/api/vault/reveal", {"vault": "html", "path": str(self.page)}, token=False).status_code, 403)
            shown = self.post("/api/vault/reveal", {"vault": "html", "path": str(self.page), "which": "real"}).json()
            link = self.post("/api/vault/reveal", {"vault": "html", "path": str(self.page), "which": "link"}).json()
            escape = self.post("/api/vault/reveal", {"vault": "html", "path": str(self.vault / ".." / "context")})
            not_a_link = self.post("/api/vault/reveal", {"vault": "html", "path": str(self.vault / "Mine"), "which": "link"})
        self.assertEqual((shown["revealed"], link["revealed"]), (real, str(self.vault / "Architect")))
        self.assertEqual([call.args[0] for call in reveal.call_args_list], [real, str(self.vault / "Architect")])
        self.assertEqual((escape.status_code, not_a_link.status_code), (400, 400))
        self.assertEqual(not_a_link.json()["error"], "That row is not a link.")

    def test_reorganising_moves_only_what_the_vault_owns_and_never_a_target(self) -> None:
        def tree() -> dict:
            return self.client.get("/api/vault/tree", params={"vault": "html"}).json()["tree"]

        def child(node: dict, name: str) -> dict:
            return next(c for c in node["children"] if c["name"] == name)

        def labels(node: dict) -> list[str]:
            return [c.get("title") or c["name"] for c in node["children"]]

        # Every row the vault owns says what moving it moves; a guide inside the linked topic says nothing.
        architect = child(tree(), "Architect")
        self.assertEqual(architect["entry"], str(self.vault / "Architect"))
        self.assertNotIn("entry", architect["children"][0]["children"][0])
        self.assertEqual(child(tree(), "Mine")["entry"], str(self.vault / "Mine"))

        # A link moves into a new folder; its target stays put and the page still reads through it.
        self.post("/api/vault/html/folder", {"parent": "", "name": "Learnings"})
        move = {"path": str(self.vault / "Architect"), "dest": "Learnings"}
        self.assertEqual(self.post("/api/vault/html/move", move, token=False).status_code, 403)
        foreign = self.client.post(
            "/api/vault/html/move", json={"token": self.config.token, **move}, headers={"origin": "https://attacker.example"}
        )
        self.assertEqual(foreign.status_code, 403)
        moved = self.post("/api/vault/html/move", move).json()
        learnings = self.vault / "Learnings"
        self.assertEqual((moved["from"], moved["path"]), (str(self.vault / "Architect"), str(learnings / "Architect")))
        self.assertFalse(os.path.lexists(self.vault / "Architect"))
        self.assertEqual(os.readlink(learnings / "Architect"), str(self.topic))
        page = learnings / "Architect" / "guides" / "who-holds-the-plan" / "index.html"
        self.assertEqual(self.client.get("/view", params={"src": str(page)}).status_code, 200)

        # Nothing inside a linked folder moves: that would carry the real guide out of its topic.
        inside = self.post("/api/vault/html/move", {"path": str(page.parent), "dest": ""})
        self.assertEqual(inside.status_code, 400)
        self.assertIn("inside a linked folder", inside.json()["error"])
        self.assertTrue((self.guide / "index.html").is_file())
        # A folder can't go inside itself, and a name already taken stays taken.
        into_itself = self.post("/api/vault/html/move", {"path": str(learnings), "dest": "Learnings"})
        self.assertIn("inside itself", into_itself.json()["error"])
        self.post("/api/vault/html/folder", {"parent": "", "name": "Architect"})
        taken = self.post("/api/vault/html/move", {"path": str(learnings / "Architect"), "dest": ""})
        self.assertIn("already exists", taken.json()["error"])
        self.assertEqual(self.post("/api/vault/html/remove", {"path": str(self.vault / "Architect")}).json()["removed"], "folder")

        # Pin to Top: a guide-folder link, one row two levels down, sorts ahead of the folders. The pin is a dotfile the
        # tree never lists, and it moves with the entry.
        dashboard = self.base / "learnings" / "dashboard"
        dashboard.mkdir()
        (dashboard / "index.html").write_text("<title>Mastery Map</title>", encoding="utf-8")
        mastery = learnings / "Mastery Map"
        mastery.symlink_to(dashboard, target_is_directory=True)
        self.assertEqual(labels(child(tree(), "Learnings")), ["Architect", "Mastery Map"])
        self.assertEqual(child(tree(), "Learnings")["children"][1]["entry"], str(mastery))
        self.post("/api/vault/html/pin", {"path": str(mastery), "pinned": True})
        self.assertEqual(labels(child(tree(), "Learnings")), ["Mastery Map", "Architect"])
        self.assertTrue(child(tree(), "Learnings")["children"][0]["pinned"])
        self.assertEqual(json.loads((learnings / ".onyx.json").read_text())["pinned"], ["Mastery Map"])
        renamed = self.post("/api/vault/html/rename", {"path": str(learnings), "name": "Study"}).json()
        study = self.vault / "Study"
        self.assertEqual(renamed["path"], str(study))
        self.assertEqual(labels(child(tree(), "Study")), ["Mastery Map", "Architect"])  # its pins live inside it
        self.post("/api/vault/html/move", {"path": str(study / "Mastery Map"), "dest": "Mine"})
        self.assertEqual(json.loads((self.vault / "Mine" / ".onyx.json").read_text())["pinned"], ["Mastery Map"])
        self.assertFalse((study / ".onyx.json").exists())
        self.post("/api/vault/html/pin", {"path": str(self.vault / "Mine" / "Mastery Map"), "pinned": False})
        self.assertFalse((self.vault / "Mine" / ".onyx.json").exists())

        # A hand-made relative link that a move would re-aim becomes absolute; one into the moved folder stays relative.
        hand = self.vault / "Mine" / "Hand"
        (hand / "sub").mkdir(parents=True)
        (hand / "sub" / "note.html").write_text("<title>Inner</title>", encoding="utf-8")
        os.symlink(os.path.relpath(dashboard / "index.html", hand), hand / "out.html")
        os.symlink("sub/note.html", hand / "in.html")
        self.post("/api/vault/html/move", {"path": str(hand), "dest": ""})
        self.assertEqual(os.readlink(self.vault / "Hand" / "out.html"), str(dashboard / "index.html"))
        self.assertEqual(os.readlink(self.vault / "Hand" / "in.html"), "sub/note.html")
        self.assertTrue((self.vault / "Hand" / "out.html").is_file() and (self.vault / "Hand" / "in.html").is_file())

        # Remove takes out a link, never its target, or an empty folder; a real file and a full folder are refused.
        self.assertIn("isn't empty", self.post("/api/vault/html/remove", {"path": str(self.vault / "Hand")}).json()["error"])
        real = self.post("/api/vault/html/remove", {"path": str(self.vault / "Hand" / "sub" / "note.html")})
        self.assertIn("real file", real.json()["error"])
        self.assertEqual(self.post("/api/vault/html/remove", {"path": str(study / "Architect")}).json()["removed"], "link")
        self.assertTrue((self.guide / "index.html").is_file())
        self.assertEqual(self.post("/api/vault/html/remove", {"path": str(study)}).json()["removed"], "folder")
        self.assertFalse(study.exists())


class PluginOriginTests(unittest.TestCase):
    """The Obsidian plugin talks to the same API from app://obsidian.md."""

    OBSIDIAN = "app://obsidian.md"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document = (self.root / "guide.md")
        self.document.write_text("# Guide\n\nThe server is local.", encoding="utf-8")
        self.document = self.document.resolve()
        self.config = AppConfig(
            default_folder=self.root,
            allowed_roots=(self.root,),
            port=8899,
            data_dir=self.root / "data",
        )
        self.app = create_app(self.config)
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8899")
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def ask(self, origin: str):
        async def fake_stream(*args, **kwargs):
            yield _sse("token", {"text": "ok"})
            yield _sse("done", {"elapsed_ms": 3})

        body = {
            "token": self.config.token,
            "action": "eli5",
            "selection": "The server is local.",
            "folder": str(self.root),
            "document_source": str(self.document),
        }
        with patch("onyx.app.stream_answer", fake_stream):
            return self.client.post("/ask", json=body, headers={"origin": origin})

    def test_obsidian_origin_is_allowed_on_ask_and_api(self) -> None:
        response = self.ask(self.OBSIDIAN)
        self.assertIn("event: meta", response.text)
        self.assertNotIn("origin not allowed", response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], self.OBSIDIAN)
        self.assertEqual(response.headers["vary"], "Origin")
        history = self.client.get(
            "/api/history", params={"source": str(self.document)}, headers={"origin": self.OBSIDIAN}
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.headers["access-control-allow-origin"], self.OBSIDIAN)

    def test_unlisted_origins_are_still_rejected(self) -> None:
        for origin in (
            "https://attacker.example",
            "app://evil.md",
            "app://obsidian.md.evil",
            "app://obsidian.md/",
        ):
            with self.subTest(origin=origin):
                blocked = self.client.get("/api/library", headers={"origin": origin})
                self.assertEqual(blocked.status_code, 403)
                self.assertNotIn("access-control-allow-origin", blocked.headers)
                stream = self.ask(origin)
                self.assertIn("Refused: origin not allowed.", stream.text)
                self.assertNotIn("access-control-allow-origin", stream.headers)

    def test_session_endpoint_returns_token_only_to_allowed_origins(self) -> None:
        allowed = self.client.get("/api/session", headers={"origin": self.OBSIDIAN})
        self.assertEqual(allowed.status_code, 200)
        payload = allowed.json()
        self.assertEqual(payload["token"], self.config.token)
        self.assertEqual(payload["service"], "onyx")
        self.assertEqual(payload["protocol"], 3)
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["request_timeout"], 120)
        self.assertEqual(allowed.headers["cache-control"], "no-store")

        blocked = self.client.get("/api/session", headers={"origin": "https://attacker.example"})
        self.assertEqual(blocked.status_code, 403)
        self.assertNotIn("token", blocked.json())

        self.assertEqual(self.client.get("/api/session").status_code, 200)

    def test_preflight_echoes_allowed_origin_and_headers(self) -> None:
        for path in ("/ask", "/api/roots"):
            with self.subTest(path=path):
                response = self.client.options(path, headers={"origin": self.OBSIDIAN})
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["access-control-allow-origin"], self.OBSIDIAN)
                self.assertIn("Content-Type", response.headers["access-control-allow-headers"])

    def test_allowed_origins_setting_is_persisted_and_validated(self) -> None:
        saved = self.client.post(
            "/api/settings",
            json={"token": self.config.token, "settings": {"allowed_origins": ["app://obsidian.md"]}},
        )
        self.assertEqual(saved.json()["settings"]["allowed_origins"], ["app://obsidian.md"])
        for bad in ("*", "app://x/path"):
            rejected = self.client.post(
                "/api/settings", json={"token": self.config.token, "settings": {"allowed_origins": [bad]}}
            )
            self.assertEqual(rejected.status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/settings", json={"token": self.config.token, "settings": {"allowed_origins": "nope"}}
            ).status_code,
            400,
        )

        cleared = self.client.post(
            "/api/settings", json={"token": self.config.token, "settings": {"allowed_origins": []}}
        )
        self.assertEqual(cleared.json()["settings"]["allowed_origins"], [])
        self.assertEqual(
            self.client.get("/api/library", headers={"origin": self.OBSIDIAN}).status_code, 403
        )
        self.assertEqual(
            self.client.get("/api/library", headers={"origin": "http://localhost:9999"}).status_code, 200
        )

    def test_health_and_plugin_routes_carry_cors_headers(self) -> None:
        headers = {"origin": self.OBSIDIAN}
        health = self.client.get("/health", headers=headers)
        self.assertEqual(health.json()["service"], "onyx")
        self.assertEqual(health.headers["access-control-allow-origin"], self.OBSIDIAN)
        for path, params in (
            ("/api/settings", None),
            ("/api/document", {"source": str(self.document)}),
        ):
            with self.subTest(path=path):
                response = self.client.get(path, params=params, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], self.OBSIDIAN)
        added = self.client.post(
            "/api/roots", json={"token": self.config.token, "path": str(self.root)}, headers=headers
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.headers["access-control-allow-origin"], self.OBSIDIAN)
        opened = self.client.post(
            "/api/open-source",
            json={"token": self.config.token, "path": str(self.document), "folder": str(self.root)},
            headers=headers,
        )
        self.assertIn("access-control-allow-origin", opened.headers)
