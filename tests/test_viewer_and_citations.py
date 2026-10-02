from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from reportlab.pdfgen import canvas

from onyx.citations import extract_citations, reader_target
from onyx.vault import VaultIndex
from onyx.launcher_ui import (
    BASE_RGB,
    READABLE_CONTRAST,
    SIDEBAR_LABEL,
    SIDEBAR_READABLE,
    SIDEBAR_TINT,
    blur_radius,
    glass_alphas,
    glass_script,
    sidebar_contrast,
    theme_style,
)
from onyx.viewer import (
    RangeNotSatisfiable,
    SourceConflict,
    ViewerError,
    byte_range,
    load_local_document,
    parse_flat_frontmatter,
    prepare_html,
    read_source,
    set_task,
    split_frontmatter,
    stat_signature,
    validate_remote_url,
    write_source,
)


class ViewerAndCitationTests(unittest.TestCase):
    def test_markdown_is_rendered_without_executing_raw_html(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "notes.md"
            path.write_text(
                "# Notes\n\n"
                "<script>alert(1)</script>\n\n"
                "- a wrapped item\n"
                "  that stays in the same list entry\n\n"
                "1. first\n"
                "2. second\n\n"
                "| Source | Result |\n"
                "| --- | --- |\n"
                "| local | safe |\n\n"
                "[unsafe](javascript:alert(1))\n",
                encoding="utf-8",
            )
            loaded = load_local_document(path)
            self.assertEqual(loaded.kind, "markdown")
            self.assertIn("<h1>Notes</h1>", loaded.html)
            self.assertNotIn("<script>alert", loaded.html)
            self.assertIn("&lt;script&gt;", loaded.html)
            self.assertIn("<li>a wrapped item\nthat stays in the same list entry</li>", loaded.html)
            self.assertIn("<ol>", loaded.html)
            self.assertIn("<table>", loaded.html)
            self.assertNotIn('href="javascript:', loaded.html)
            self.assertIn("prefers-color-scheme:dark", loaded.html)
            self.assertIn("backdrop-filter:blur(22px)", loaded.html)

    def test_frontmatter_becomes_a_properties_block(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "note.md"
            path.write_text(
                "---\n"
                "title: \"Quoted title\"\n"
                "tags: [career, 'ai-lab']\n"
                "aliases:\n"
                "  - Trusted Counselor\n"
                "  - TC\n"
                "status:\n"
                "created: 2026-07-02\n"
                "# a comment\n"
                "---\n\n"
                "# Real heading\n\nBody text.\n",
                encoding="utf-8",
            )
            loaded = load_local_document(path)
            self.assertEqual(loaded.title, "Real heading")
            self.assertIn('<details class="askw-properties"><summary>Properties · 5</summary>', loaded.html)
            self.assertIn('<span class="askw-tag">#career</span><span class="askw-tag">#ai-lab</span>', loaded.html)
            self.assertIn("<dd>Trusted Counselor, TC</dd>", loaded.html)
            self.assertIn('<dt>status</dt><dd><span class="askw-empty">—</span></dd>', loaded.html)
            self.assertIn("<dd>Quoted title</dd>", loaded.html)
            self.assertNotIn("<hr", loaded.html)
            self.assertNotIn("created: 2026", loaded.html)

            path.write_text("---\ntitle: From frontmatter\n---\n\nNo heading here.\n", encoding="utf-8")
            self.assertEqual(load_local_document(path).title, "From frontmatter")

            path.write_text("---\nnested:\n  deep: value\n---\n\n# Nested\n", encoding="utf-8")
            nested = load_local_document(path)
            self.assertIn('<details class="askw-properties"><summary>Properties</summary><pre>', nested.html)
            self.assertIn("deep: value", nested.html)
            self.assertEqual(nested.title, "Nested")

            path.write_text("---\n\nJust a rule, then prose\n\n---\n\nmore\n", encoding="utf-8")
            rule = load_local_document(path)
            self.assertEqual(rule.html.count("<hr />"), 2)
            self.assertIn("Just a rule, then prose", rule.html)
            self.assertEqual(split_frontmatter("---\n---\nbody")[0], "")
            self.assertIsNone(parse_flat_frontmatter("a:\n  b: c"))

    def test_relative_document_links_rewrite_to_the_reader(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "notes").mkdir()
            path = root / "notes" / "Index.md"
            path.write_text(
                "[x](How%20to.md) [y](../B.md#sec) [site](https://example.com/a.md) "
                "[img](notes.png) [f](file:///tmp/nope.md) [same](#local)\n",
                encoding="utf-8",
            )
            loaded = load_local_document(path, folder=str(root))
            self.assertIn(
                f'href="/view?src={quote(str(root / "notes" / "How to.md"))}&amp;folder={quote(str(root))}"',
                loaded.html,
            )
            self.assertIn(f'href="/view?src={quote(str(root / "B.md"))}&amp;folder={quote(str(root))}#sec"', loaded.html)
            self.assertIn('href="https://example.com/a.md" rel="noreferrer noopener" target="_top"', loaded.html)
            self.assertIn('href="notes.png"', loaded.html)
            self.assertIn('href="file:///tmp/nope.md"', loaded.html)  # absent file stays untouched
            self.assertIn('href="#local"', loaded.html)

    def test_saving_a_note_keeps_its_file_its_line_endings_and_its_byte_order_mark(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw = str(Path(raw).resolve())  # a capability names the realpath
            path = Path(raw) / "note.md"
            path.write_bytes(codecs.BOM_UTF8 + b"# Title\r\n\r\nBody\r\n")
            text, sig = read_source(path)
            self.assertEqual(text, "# Title\n\nBody\n")  # as the editor keeps text
            inode = path.stat().st_ino
            written = write_source(path, "# Title\n\nNew body\n", base=sig)
            self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"# Title\r\n\r\nNew body\r\n")
            # The same file, not a new one renamed over it: Obsidian's created date is the file's.
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(written, stat_signature(path.stat()))
            # Written against a version that is no longer on disk: refused, and nothing written.
            with self.assertRaises(SourceConflict) as refused:
                write_source(path, "clobber", base=sig)
            self.assertEqual(refused.exception.sig, written)
            # A path that is a symlink now is refused, not followed.
            link = Path(raw) / "link.md"
            link.symlink_to(path)
            with self.assertRaises(ViewerError):
                write_source(link, "clobber", base=written)
            self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"# Title\r\n\r\nNew body\r\n")
            # Text that isn't UTF-8 would be written back as replacement characters, so it isn't opened.
            latin = Path(raw) / "latin.md"
            latin.write_bytes(b"caf\xe9")
            with self.assertRaises(ViewerError):
                read_source(latin)
            page = Path(raw) / "page.html"
            page.write_text("<p>x</p>", encoding="utf-8")
            with self.assertRaises(ViewerError):
                read_source(page)

    def test_notes_render_obsidians_strikethrough_highlights_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw = str(Path(raw).resolve())  # a capability names the realpath
            path = Path(raw) / "tasks.md"
            path.write_text(
                "---\ntags: [x]\n---\n\n- [ ] open ~~gone~~ ==marked==\n- [x] done\n- plain [ ] item\n\nnot a == highlight\n",
                encoding="utf-8",
            )
            html = load_local_document(path).html
            self.assertIn("<s>gone</s> <mark>marked</mark>", html)
            self.assertIn("not a == highlight", html)
            # A box carries its line in the file, frontmatter included, for a click to tick it.
            self.assertIn('<li class="askw-task"><input type="checkbox" class="askw-task-box" data-askw-line="4" aria-label="Task">'
                          '<span class="askw-task-label">open', html)
            self.assertIn('<li class="askw-task is-done"><input type="checkbox" class="askw-task-box" checked data-askw-line="5"', html)
            self.assertIn("<li>plain [ ] item</li>", html)

            text, sig = read_source(path)
            sig = set_task(path, 4, True, base=sig)
            self.assertIn("- [x] open ~~gone~~", path.read_text(encoding="utf-8"))
            sig = set_task(path, 5, False, base=sig)
            self.assertIn("- [ ] done", path.read_text(encoding="utf-8"))
            with self.assertRaises(ViewerError):
                set_task(path, 6, True, base=sig)  # not a task
            with self.assertRaises(SourceConflict):
                set_task(path, 4, False, base="stale")

    def test_notes_render_obsidians_callouts_comments_tags_bare_urls_and_line_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            vault.mkdir()
            note = vault / "Note.md"
            note.write_text(
                "> [!info] A **bold** title\n> first line\n> second line\n\n"
                "> [!faq]- Shut\n> > [!tip]\n> > inner\n\n"
                "> [!Warning]+\n\n> plain quote\n\n"
                "seen %%unseen%% seen\n\n%%\nsecretone\n\nsecrettwo\n%%\n\n"
                "a #tag, #nested/tag, #1 and page#frag\n\n"
                "go to https://obsidian.md/help. or (https://en.wikipedia.org/wiki/A_(b)) not `https://code`\n\n"
                "```\n%% code %%\n```\n",
                encoding="utf-8",
            )
            html = load_local_document(note, folder=str(vault), vault=VaultIndex.build(vault)).html
            self.assertIn('<div class="callout" data-callout="info">', html)
            self.assertIn('<span class="callout-title-inner">A <strong>bold</strong> title</span>', html)
            self.assertIn("<p>first line<br>\nsecond line</p>", html)  # a vault note's newline is a break, as in Obsidian
            self.assertIn('<details class="callout is-collapsible is-collapsed" data-callout="faq">', html)
            self.assertIn('<span class="callout-title-inner">Tip</span>', html)  # no title: the type's
            self.assertIn('<details class="callout is-collapsible" data-callout="warning" open>', html)
            self.assertIn('<span class="callout-title-inner">Warning</span>', html)
            self.assertIn("<blockquote>\n<p>plain quote</p>", html)
            self.assertIn("seen  seen", html)
            self.assertNotIn("unseen", html)
            self.assertNotIn("secret", html)
            self.assertIn("<code>%% code %%\n</code>", html)
            self.assertIn('<span class="askw-tag">#tag</span>, <span class="askw-tag">#nested/tag</span>, #1 and page#frag', html)
            self.assertIn('<a href="https://obsidian.md/help" rel="noreferrer noopener" target="_top">https://obsidian.md/help</a>.', html)
            self.assertIn('href="https://en.wikipedia.org/wiki/A_(b)"', html)
            self.assertIn("<code>https://code</code>", html)

            # Outside a vault: CommonMark's soft break, and a `#` is text.
            plain = load_local_document(note).html
            self.assertIn("<p>first line\nsecond line</p>", plain)
            self.assertIn("a #tag, #nested/tag", plain)

    def test_two_saves_against_one_version_never_both_land(self) -> None:
        # Two tabs (or a tick beside an autosave) saving over the same version: one lands, the other is refused. Each
        # thread is held at its version check until the other reaches its own, or half a second passes: unlocked, both
        # pass the check together and both "succeed", one overwriting the other.
        import threading
        from unittest.mock import patch

        from onyx import viewer

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw).resolve() / "race.md"
            path.write_text("# Race\n", encoding="utf-8")
            _, sig = read_source(path)
            meet, real, outcomes = threading.Barrier(2, timeout=0.5), viewer.stat_signature, []

            def held(st):
                if getattr(held_here, "first", True):
                    held_here.first = False
                    try:
                        meet.wait()
                    except threading.BrokenBarrierError:
                        pass
                return real(st)
            held_here = threading.local()

            def save(text):
                try:
                    write_source(path, text, base=sig)
                    outcomes.append("saved")
                except SourceConflict:
                    outcomes.append("refused")

            with patch.object(viewer, "stat_signature", held):
                threads = [threading.Thread(target=save, args=(f"# Race\n\n{who}\n",)) for who in ("one", "two")]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(sorted(outcomes), ["refused", "saved"])
            self.assertIn(path.read_text(encoding="utf-8"), ("# Race\n\none\n", "# Race\n\ntwo\n"))

    def test_a_note_is_saved_only_where_its_page_found_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            (base / "notes").mkdir()
            (base / "elsewhere").mkdir()
            path = base / "notes" / "note.md"
            path.write_text("# Mine\n", encoding="utf-8")
            (base / "elsewhere" / "note.md").write_text("# Someone else's\n", encoding="utf-8")
            _, sig = read_source(path)
            # The folder swapped for a symlink to another with a note of the same name: the same path now leads there,
            # and neither a read nor a save follows it.
            (base / "notes").rename(base / "moved")
            (base / "notes").symlink_to(base / "elsewhere", target_is_directory=True)
            with self.assertRaises(ViewerError):
                read_source(path)
            with self.assertRaises(ViewerError):
                write_source(path, "# Overwritten\n", base=sig)
            self.assertEqual((base / "elsewhere" / "note.md").read_text(encoding="utf-8"), "# Someone else's\n")

    def test_a_note_replaced_while_it_is_saved_is_a_conflict_not_a_save(self) -> None:
        # A sync renaming its own copy over the note during the write: the bytes went to the file it replaced, so the
        # save must not report success (the editor would call its text saved, then take the disk's over it).
        from unittest.mock import patch

        from onyx import viewer

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw).resolve() / "synced.md"
            path.write_text("# Before\n", encoding="utf-8")
            _, sig = read_source(path)
            real_fsync = viewer.os.fsync

            def sync_lands(fd):
                real_fsync(fd)
                incoming = path.with_name("synced.tmp")
                incoming.write_text("# From the sync\n", encoding="utf-8")
                incoming.replace(path)

            with patch.object(viewer.os, "fsync", sync_lands), self.assertRaises(SourceConflict) as refused:
                write_source(path, "# Mine\n", base=sig)
            self.assertEqual(path.read_text(encoding="utf-8"), "# From the sync\n")
            self.assertEqual(refused.exception.sig, stat_signature(path.stat()))

    def test_wikilinks_render_only_in_vault_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            (vault / "Other" / "Attachments").mkdir(parents=True)
            alpha = vault / "Alpha.md"
            alpha.write_text(
                "[[Beta]] and [[Beta|the alias]] and [[Beta#Part]] and `[[Beta]]` and [[Nowhere]] "
                "![[Pasted image.png]] ![[Beta]] ![[song.mp3]]\n",
                encoding="utf-8",
            )
            (vault / "Beta.md").write_text("# Beta\n", encoding="utf-8")
            (vault / "Other" / "Attachments" / "Pasted image.png").write_bytes(b"png")
            (vault / "song.mp3").write_bytes(b"mp3")
            index = VaultIndex.build(vault)
            loaded = load_local_document(alpha, folder=str(vault), vault=index)
            beta = quote(str(vault / "Beta.md"))
            self.assertIn(f'<a href="/view?src={beta}&amp;folder={quote(str(vault))}" class="askw-wikilink" title="Beta.md" rel="noreferrer noopener">Beta</a>', loaded.html)
            self.assertIn('class="askw-wikilink" title="Beta.md" rel="noreferrer noopener">the alias</a>', loaded.html)
            self.assertIn(f'href="/view?src={beta}&amp;folder={quote(str(vault))}#Part"', loaded.html)
            self.assertIn("<code>[[Beta]]</code>", loaded.html)
            self.assertIn('<span class="askw-wikilink-missing" title="No note named “Nowhere”">Nowhere</span>', loaded.html)
            self.assertIn(f'<img src="{quote(str(vault / "Other" / "Attachments" / "Pasted image.png"))}" alt="Pasted image.png" />', loaded.html)
            self.assertIn('class="askw-wikilink askw-embed" title="Beta.md" rel="noreferrer noopener">Beta.md</a>', loaded.html)
            self.assertIn('<span class="askw-wikilink-missing" title="Unsupported embed">song.mp3</span>', loaded.html)

            plain = load_local_document(alpha, folder=str(vault))
            self.assertIn("[[Beta]] and [[Beta|the alias]]", plain.html)
            self.assertNotIn('class="askw-wikilink', plain.html)

            html, assets = prepare_html(
                str(alpha),
                html_text=loaded.html,
                server_origin="http://127.0.0.1:8899",
                folder=str(vault),
                asset_token="cap",
            )
            self.assertIn("/_fs/cap/", html)
            self.assertEqual(assets, {str((vault / "Other" / "Attachments" / "Pasted image.png").resolve())})

    def test_percent_encoded_relative_images_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "Pasted image.png"
            image.write_bytes(b"png")
            html, assets = prepare_html(
                str(root / "doc.html"),
                html_text='<html><body><img src="Pasted%20image.png"></body></html>',
                server_origin="http://127.0.0.1:8899",
                folder=str(root),
                asset_token="cap",
            )
            self.assertEqual(assets, {str(image.resolve())})
            self.assertIn("/_fs/cap" + quote(str(image.resolve())), html)

    def test_pdf_reader_preserves_page_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "two-pages.pdf"
            writer = canvas.Canvas(str(path), pagesize=(612, 792))
            writer.setTitle("Two Pages")
            writer.drawString(72, 720, "Evidence on the first page")
            writer.showPage()
            writer.drawString(72, 720, "Evidence on the second page")
            writer.save()
            loaded = load_local_document(path)
            self.assertEqual(loaded.kind, "pdf")
            self.assertEqual(loaded.page_count, 2)
            self.assertIn('data-askw-page="1"', loaded.html)
            self.assertIn('data-askw-page="2"', loaded.html)
            self.assertIn("Evidence on the first page", loaded.html)
            self.assertIn("Evidence on the second page", loaded.html)

    def test_remote_private_addresses_are_rejected(self) -> None:
        with self.assertRaises(ViewerError):
            validate_remote_url("http://127.0.0.1/private")
        self.assertEqual(
            validate_remote_url("http://127.0.0.1/private", allow_private=True),
            "http://127.0.0.1/private",
        )

    def test_document_assets_use_a_capability_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = root / "doc.html"
            image = root / "image.png"
            image.write_bytes(b"png")
            html, assets = prepare_html(
                str(doc),
                html_text='<html><body><img src="image.png"></body></html>',
                server_origin="http://127.0.0.1:8899",
                folder=str(root),
                asset_token="capability",
            )
            self.assertIn("/_fs/capability/", html)
            self.assertIn('name="askw-doc-token" content="capability"', html)
            self.assertEqual(assets, {str(image.resolve())})

    def test_trusted_local_html_keeps_interactive_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = root / "interactive.html"
            behavior = root / "behavior.js"
            behavior.write_text("window.externalButtonReady = true", encoding="utf-8")
            html, assets = prepare_html(
                str(doc),
                html_text=(
                    '<html><body><button onclick="setLevel(\'elii\')">ELII</button>'
                    '<script>window.setLevel = function () { return true }</script>'
                    '<script src="behavior.js"></script></body></html>'
                ),
                server_origin="http://127.0.0.1:8899",
                folder=str(root),
                asset_token="capability",
                allow_document_scripts=True,
            )
            self.assertIn('onclick="setLevel(\'elii\')"', html)
            self.assertIn("window.setLevel", html)
            self.assertIn("/_fs/capability/", html)
            self.assertEqual(assets, {str(behavior.resolve())})

    def test_remote_html_scripts_stay_inert_even_if_requested(self) -> None:
        html, _ = prepare_html(
            "https://example.com/article",
            html_text='<html><body onclick="alert(1)"><script>alert(1)</script></body></html>',
            server_origin="http://127.0.0.1:8899",
            folder=None,
            allow_document_scripts=True,
        )
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_citations_are_validated_and_include_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("one\ntwo\nimportant evidence\nfour\n", encoding="utf-8")
            outside = root.parent / "not-allowed.py"
            answer = f"See `src/app.py:3` and `{outside}:1`."
            citations = extract_citations(answer, root)
            self.assertEqual(len(citations), 1)
            self.assertEqual(citations[0]["label"], "src/app.py:3")
            self.assertIn("important evidence", citations[0]["snippet"])

    def test_a_line_written_in_words_is_still_the_cited_line(self) -> None:
        # "`index.html` (line 1084)" fell back to line 1, and the preview showed <!DOCTYPE html>.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "guides").mkdir()
            (root / "guides" / "trace.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            (root / "guides" / "page.html").write_text("<p>one</p>\n<p>two</p>\n<p>three</p>\n", encoding="utf-8")
            for answer in ("claim row in `guides/trace.md` (line 3).", "see `guides/trace.md`, lines 3-4", "(guides/trace.md line 3)"):
                with self.subTest(answer=answer):
                    self.assertEqual(extract_citations(answer, root)[0]["line"], 3)
            # Words that only look like a line are not one (a page with no line cited has none).
            self.assertIsNone(extract_citations("`guides/page.html` lines up with it", root)[0]["line"])

    def test_page_evidence_previews_its_words_and_says_where_the_reader_lands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            page = root / "guide.html"
            page.write_text(
                "<!DOCTYPE html>\n"
                '<html lang="en">\n'
                '<head><style>[id="x"]{color:red}</style></head>\n'
                '<body><section id="sources">\n'
                "<details><summary>Claims</summary><table>\n"
                "<tr><td>rtt-05</td><td>Streaming uses block deltas &amp; block-stop events (then message_stop).</td></tr>\n"
                "<tr>\n"
                "<td>\n"
                "</td>\n"
                "</tr>\n"
                "<tr><td>rtt-06</td><td>Match one result per call.</td></tr>\n"
                "</table></details></section></body></html>\n",
                encoding="utf-8",
            )
            words = "rtt-05 Streaming uses block deltas & block-stop events (then message_stop)."
            cited = extract_citations("claim rtt-05 in `guide.html` (line 6)", root)[0]
            self.assertEqual((cited["label"], cited["snippet"]), ("guide.html:6", words))
            # With no line to point at, a page has no preview: its first lines are only <!DOCTYPE html> and <head>.
            bare = extract_citations("see `guide.html`", root)[0]
            self.assertEqual((bare["label"], bare["snippet"], bare["line"]), ("guide.html", "", None))

            self.assertEqual(reader_target(page, line=6), {"text": words, "anchor": "sources", "page": None})
            # A line of bare markup reads on until there are words to find it by.
            self.assertEqual(reader_target(page, line=7)["text"], "rtt-06 Match one result per call.")
            # A stylesheet's [id="x"] is not an anchor, and no line means nowhere in particular.
            self.assertIsNone(reader_target(page, line=3)["anchor"])
            self.assertEqual(reader_target(page), {"text": "", "anchor": None, "page": None})

            note = root / "note.md"
            note.write_text("# Title\n\n- The **plan** lives in [[Harness|the harness]], see [docs](https://x.y).\n", encoding="utf-8")
            self.assertEqual(reader_target(note, line=3)["text"], "The plan lives in the harness, see docs.")
            self.assertEqual(reader_target(root / "slides.pdf", page=2), {"text": "", "anchor": None, "page": 2})

    def test_evidence_keeps_the_words_it_cited_when_the_page_moves_on(self) -> None:
        # A guide gained 46 lines between an answer and its evidence being opened: line 1084 no longer held rtt-05,
        # and re-reading the line at open time landed somewhere else on the page.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            page = root / "guide.html"
            page.write_text('<section id="sources">\n<p>Intro.</p>\n<p>rtt-05 Streaming uses block deltas.</p>\n</section>\n', encoding="utf-8")
            cited = extract_citations("`guide.html` (line 3)", root)[0]
            self.assertEqual((cited["text"], cited["anchor"]), ("rtt-05 Streaming uses block deltas.", "sources"))
            page.write_text("<p>A lead added later.</p>\n<p>And another.</p>\n" + page.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertNotEqual(reader_target(page, line=3)["text"], cited["text"])  # the line moved on; the kept words didn't
            # No line cited, nothing to land on: a page opens where the reader left it.
            self.assertNotIn("text", extract_citations("see `guide.html`", root)[0])


class RangeAndGlassTests(unittest.TestCase):
    def test_byte_range_follows_rfc_9110_for_single_ranges(self) -> None:
        self.assertIsNone(byte_range(None, 10))
        self.assertEqual(byte_range("bytes=0-1", 10), (0, 1))
        self.assertEqual(byte_range("bytes=5-", 10), (5, 9))
        self.assertEqual(byte_range("bytes=-3", 10), (7, 9))
        self.assertEqual(byte_range("bytes=-20", 10), (0, 9))
        self.assertEqual(byte_range("bytes=8-100", 10), (8, 9))
        self.assertEqual(byte_range(" Bytes = 2 - 4 ", 10), (2, 4))
        for ignored in ("items=0-1", "bytes=0-1,3-4", "bytes=abc", "bytes=-", "bytes=3-1", "bytes", "bytes=1"):
            self.assertIsNone(byte_range(ignored, 10), ignored)
        for unsatisfiable, size in (("bytes=10-", 10), ("bytes=10-12", 10), ("bytes=-0", 10), ("bytes=0-", 0)):
            with self.assertRaises(RangeNotSatisfiable, msg=unsatisfiable):
                byte_range(unsatisfiable, size)

    def test_media_tags_are_rewritten_to_capability_urls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "audio").mkdir()
            for name in ("one.m4a", "clip.webm", "en.vtt"):
                (root / "audio" / name).write_bytes(b"x")
            html, assets = prepare_html(
                str(root / "guide.html"),
                html_text=(
                    '<body><audio controls preload="none" src="audio/one.m4a"></audio>'
                    "<video src='audio/clip.webm'><track src=\"audio/en.vtt\"></video></body>"
                ),
                server_origin="http://127.0.0.1:8899",
                folder=str(root),
                asset_token="cap",
            )
            self.assertEqual(
                assets, {str((root / "audio" / n).resolve()) for n in ("one.m4a", "clip.webm", "en.vtt")}
            )
            self.assertNotIn('src="audio/one.m4a"', html)
            self.assertIn('<audio controls preload="none" src="http://127.0.0.1:8899/_fs/cap/', html)

    def test_blur_radius_is_the_cxtasks_curve(self) -> None:
        self.assertEqual(blur_radius(0), 10)
        self.assertEqual(blur_radius(0.38), 24)  # the default slider lands where cxtasks pins it
        self.assertEqual(blur_radius(1), 48)
        self.assertEqual(blur_radius(7), 48)
        self.assertEqual(blur_radius(float("nan")), 10)  # a corrupt value must not reach the bridge as NaN
        script = glass_script({"glass_transparency": 38})
        self.assertIn("Math.round(10+t*(48-10))", script)
        self.assertIn("requestAnimationFrame(()=>requestAnimationFrame(startGlass))", script)
        self.assertIn("window.askwReduceTransparency=", script)
        # The opaque colours the page sends must be the --bg-primary tokens it paints.
        style = theme_style({})
        for theme, (r, g, b) in BASE_RGB.items():
            self.assertIn(f"[{r},{g},{b}]", script, theme)
            self.assertIn(f"--bg-primary:{r} {g} {b}", style, theme)

    def test_sidebar_labels_stay_readable_over_any_backdrop(self) -> None:
        for theme in ("dark", "light"):
            floor = SIDEBAR_READABLE[theme]
            # The floor is the tightest alpha that clears AA, not a padded guess.
            self.assertGreaterEqual(sidebar_contrast(floor, theme), READABLE_CONTRAST, theme)
            self.assertLess(sidebar_contrast(floor - 0.01, theme), READABLE_CONTRAST, theme)
            for step in range(101):
                sidebar = glass_alphas(step / 100, theme == "dark")[1]
                self.assertGreaterEqual(sidebar, floor, (theme, step))
                self.assertGreaterEqual(sidebar_contrast(sidebar, theme), READABLE_CONTRAST, (theme, step))
        self.assertEqual(SIDEBAR_READABLE, {"dark": 0.79, "light": 0.84})
        self.assertEqual(glass_alphas(0, True), (1, 1, 1))  # opaque still means opaque
        # The floor is derived from these tokens; if the palette moves, so must they.
        style = theme_style({})
        self.assertIn("--bg-sidebar:42 43 43", style)
        self.assertEqual(SIDEBAR_TINT["dark"], 43)
        self.assertIn(f"--bg-sidebar:{SIDEBAR_TINT['light']} {SIDEBAR_TINT['light']} {SIDEBAR_TINT['light']}", style)
        for theme in ("dark", "light"):
            label = SIDEBAR_LABEL[theme]
            self.assertIn(f"--secondary:{label} {label} {label}", style)
        self.assertIn("dark?0.79:0.84", glass_script({}))
