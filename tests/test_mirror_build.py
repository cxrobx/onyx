from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onyx import viewer
from onyx.mirror import build
from onyx.mirror.build import build_mirror, markdown_theme_css
from onyx.storage import Storage

INCLUDE = ["Notes/Areas", "Artifacts"]
EXCLUDE = ["Notes/Areas/Private", "Artifacts/Private"]
# Words that stand for content which must never reach a built object when its note stays out of the mirror.
CANARIES = ("CANARY-SECRET", "CANARY-HIDDEN", "CANARY-QUOTED", "CANARY-BOM", "CANARY-NESTED", "CANARY-SIBLING", "CANARY-DOT")


def fake_ids(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


ALPHA = """---
tags: [demo]
---
# Alpha

Wikilinks: [[Beta]], [[Gamma#Part One]], [[Secret Plan]], [[Secret Plan#Budget|the budget]].

Links: [relative](Beta.md#top), [excluded](Private/Secret%20Plan.md#budget), [remote](https://example.com/x?a=1&b=2),
[mail](mailto:a@example.com), [sheet](data.csv), [art](ELSEWHERE/other.html).

![[pic.png]]

![again](../Other/Attachments/pic.png)

![gone](nope.png)
"""

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>The Page</title>
<base href="https://example.com/base/">
<link rel="stylesheet" href="style.css">
<link rel="prefetch" href="data.json">
<script src="app.js"></script>
</head><body>
<h1>Hello &amp; welcome</h1>
<img src="pic.png" alt="">
<img src="gone.png" alt="">
<a href="other.html#sec">other</a> <a href="hidden.html">hidden</a> <a href="data.json">data</a>
<a href="https://example.com">remote</a>
<script>const tpl = '<a href="' + url + '">x</a>'; const img = `<img src="${src}">`; document.title = 'kept';</script>
<style>a::after{content:"<a href='x'>"}</style>
</body></html>"""


class MirrorBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.notes = self.base / "notes"
        self.art = self.base / "artifacts"
        self.elsewhere = self.base / "elsewhere"
        self.make_notes()
        self.make_artifacts()

    def make_notes(self) -> None:
        n = self.notes
        write(n / "Areas" / "Alpha.md", ALPHA.replace("ELSEWHERE", str(self.elsewhere)))
        write(n / "Areas" / "Beta.md", "# Beta\n\nBack to [[Alpha]]. ![rel](../Other/Attachments/pic.png)\n")
        write(n / "Areas" / "Gamma.md", "# Gamma\n\n## Part One\n\n![[gamma-only.png]]\n")
        write(n / "Areas" / "Plain.txt", "line one <b>\n")
        write(n / "Areas" / "Hidden.md", "---\nmirror: false\n---\n# Hidden\nCANARY-HIDDEN\n")
        write(n / "Areas" / "Quoted.md", '---\nMirror: "No"\n---\n# Quoted\nCANARY-QUOTED\n')
        write(n / "Areas" / "Bom.md", "﻿---\nmirror: false\n---\n# Bom\nCANARY-BOM\n")
        write(n / "Areas" / "Nested.md", "---\nmirror: false\nmeta:\n  a: 1\n---\n# Nested\nCANARY-NESTED\n")
        write(n / "Areas" / "Private" / "Secret Plan.md", "# Secret Plan\n\nCANARY-SECRET\n\n## Budget\n\n![[private-only.png]]\n")
        write(n / "Areas" / "Private Extras" / "Keep.md", "# Keep\n")
        write(n / "Areas" / ".hidden" / "Note.md", "# Dot\nCANARY-DOT\n")
        write(n / "Areas" / ".dotnote.md", "# Dot\nCANARY-DOT\n")
        write(n / "Areas2" / "Sibling.md", "# Sibling\nCANARY-SIBLING\n")
        write(n / ".obsidian" / "app.json", "{}")
        for name in ("pic.png", "unused.png", "private-only.png", "gamma-only.png"):
            write(n / "Other" / "Attachments" / name, f"png:{name}".encode())
        (n / "Other").mkdir(exist_ok=True)
        os.symlink("../Areas/Alpha.md", n / "Other" / "alias.md")  # lives outside the include, points inside it

    def make_artifacts(self) -> None:
        e = self.elsewhere
        write(e / "page.html", PAGE)
        write(e / "other.html", "<title>Other Page</title><p>other</p>")
        write(e / "hidden.html", "<title>Hidden Page</title>")
        write(e / "secret.html", '<title>Secret</title><img src="secret.png">')
        write(e / "style.css", "body{color:red}")
        write(e / "app.js", "console.log(1)")
        write(e / "pic.png", b"png:page")
        write(e / "secret.png", b"png:secret")
        write(e / "unused.png", b"png:unused")
        write(e / "data.json", '{"token": "x"}')
        for folder, name in (("Project", "page.html"), ("Project", "other.html"), ("Private", "secret.html")):
            (self.art / folder).mkdir(parents=True, exist_ok=True)
            os.symlink(e / name, self.art / folder / name)
        guide = self.art / "Guides" / "g1"
        write(
            guide / "index.html",
            '<title>Guide One</title><audio src="audio/x.m4a"></audio>'
            '<a href="index.inline.html">inline</a> <a href="../../Project/other.html">other</a>',
        )
        write(guide / "index.inline.html", "<title>Inline twin</title>")
        write(guide / "audio" / "x.m4a", b"audio:x")
        write(guide / "audio" / "unused.m4a", b"audio:unused")
        write(guide / "notes.json", "{}")
        write(self.art / "Guides" / "g2" / "index.html", "<p>no title here</p>")

    # MARK: helpers

    def build(self, include: list[str] | None = None, exclude: list[str] | None = None, **kwargs):
        return build_mirror(
            roots={"Notes": self.notes, "Artifacts": self.art},
            include=INCLUDE if include is None else include,
            exclude=EXCLUDE if exclude is None else exclude,
            ids=fake_ids,
            **{"home": self.base, **kwargs},  # the temp folder stands in for the home folder assets must stay inside
        )

    @staticmethod
    def pages(built) -> dict:
        return {obj.page["path"]: obj for obj in built if obj.page is not None}

    @staticmethod
    def assets(built) -> dict:
        return {obj.name: obj for obj in built if obj.page is None}

    def html(self, built, path: str) -> str:
        return self.pages(built)[path].data.decode("utf-8")

    def asset_name(self, path: Path) -> str:
        return f"asset:{path.resolve()}"

    @staticmethod
    def snapshot(built) -> list:
        return [(obj.name, obj.id, obj.data, obj.mime, obj.page) for obj in built]

    # MARK: membership (gate 3)

    def test_mirror_publishes_only_included_folders(self) -> None:
        built = self.build()
        self.assertEqual(
            set(self.pages(built)),
            {
                "Notes/Areas/Alpha.md",
                "Notes/Areas/Beta.md",
                "Notes/Areas/Gamma.md",
                "Notes/Areas/Plain.txt",
                "Notes/Areas/Private Extras/Keep.md",  # "Private" is a prefix of it, but not a path component
                "Artifacts/Project/page.html",
                "Artifacts/Project/other.html",
                "Artifacts/Guides/g1",  # a guide folder stands where its folder does
                "Artifacts/Guides/g2",
            },
        )
        # Not Areas2 (a string-prefix sibling of Areas), not the alias that lives outside the include but points
        # into it, not the excluded folders; and nothing of them got into the pages that stayed.
        everything = b"".join(obj.data for obj in built).decode("utf-8", errors="replace")
        for canary in CANARIES:
            self.assertNotIn(canary, everything)
        self.assertNotIn("secret.png", everything)
        for obj in built:
            self.assertEqual(obj.id, fake_ids(obj.name))

        # Components, not a string prefix: "Notes/Area" is not "Notes/Areas".
        self.assertEqual(list(self.build(include=["Notes/Area"])), [])
        # There is no default list, and an entry that names nothing names nothing.
        self.assertEqual(list(self.build(include=[])), [])
        self.assertEqual(list(self.build(include=["", "/", "."])), [])
        # One page can be named on its own.
        self.assertEqual(set(self.pages(self.build(include=["Notes/Areas/Alpha.md"], exclude=[]))), {"Notes/Areas/Alpha.md"})
        # A name typed in another case, or decomposed, still excludes: a miss would publish the very thing named.
        folded = self.build(exclude=["notes/areas/PRIVATE extras", "Artifacts/guides/G1/index.html"])
        self.assertNotIn("Notes/Areas/Private Extras/Keep.md", self.pages(folded))
        self.assertNotIn("Artifacts/Guides/g1", self.pages(folded))

        # The whole notes vault still never reaches a dot-folder, an opted-out note, or the hidden file.
        whole = self.pages(self.build(include=["Notes"], exclude=[]))
        self.assertIn("Notes/Areas2/Sibling.md", whole)
        self.assertIn("Notes/Areas/Private/Secret Plan.md", whole)
        self.assertIn("Notes/Other/alias.md", whole)
        for path in whole:
            self.assertFalse(any(part.startswith(".") for part in path.split("/")), path)
        for name in ("Hidden", "Quoted", "Bom", "Nested"):
            self.assertNotIn(f"Notes/Areas/{name}.md", whole)
        everything = b"".join(obj.data for obj in whole.values()).decode("utf-8", errors="replace")
        for canary in ("CANARY-HIDDEN", "CANARY-QUOTED", "CANARY-BOM", "CANARY-NESTED", "CANARY-DOT"):
            self.assertNotIn(canary, everything)

    def test_a_root_no_include_reaches_is_never_walked(self) -> None:
        with mock.patch.object(build.vault.VaultIndex, "build", wraps=build.vault.VaultIndex.build) as walked:
            self.build(include=["Artifacts"])
        self.assertEqual([call.args[1] for call in walked.call_args_list], ["html"])

    # MARK: assets (gate 4)

    def test_mirror_uploads_only_assets_a_page_references(self) -> None:
        built = self.build()
        attachments = self.notes / "Other" / "Attachments"
        self.assertEqual(
            set(self.assets(built)),
            {
                self.asset_name(attachments / "pic.png"),  # embedded and linked three times, uploaded once
                self.asset_name(attachments / "gamma-only.png"),
                self.asset_name(self.elsewhere / "pic.png"),
                self.asset_name(self.elsewhere / "style.css"),
                self.asset_name(self.elsewhere / "app.js"),
                self.asset_name(self.art / "Guides" / "g1" / "audio" / "x.m4a"),
            },
        )
        assets = self.assets(built)
        pic = assets[self.asset_name(attachments / "pic.png")]
        self.assertEqual((pic.data, pic.mime, pic.page), (b"png:pic.png", "image/png", None))
        self.assertEqual(assets[self.asset_name(self.elsewhere / "app.js")].mime, "application/javascript")
        self.assertEqual(assets[self.asset_name(self.elsewhere / "style.css")].mime, "text/css")
        self.assertEqual(assets[self.asset_name(self.art / "Guides" / "g1" / "audio" / "x.m4a")].mime, "audio/mp4")
        # Not the files beside them, not JSON however it is referenced, and not what only an excluded page uses.
        names = " ".join(assets)
        for left_out in ("unused.png", "unused.m4a", "data.json", "notes.json", "private-only.png", "secret.png"):
            self.assertNotIn(left_out, names)

        alpha, page = self.html(built, "Notes/Areas/Alpha.md"), self.html(built, "Artifacts/Project/page.html")
        pic_id = pic.id
        self.assertEqual(alpha.count(f'src="{pic_id}"'), 2)
        self.assertEqual(alpha.count('src="#onyx-missing"'), 1)  # a file that isn't there is left dangling, not fatal
        self.assertIn(f'src="{assets[self.asset_name(self.elsewhere / "pic.png")].id}"', page)
        self.assertIn('src="#onyx-missing"', page)
        self.assertIn('rel="prefetch" href="#onyx-missing"', page)  # a JSON reference is never served
        self.assertIn(f'href="{assets[self.asset_name(self.elsewhere / "style.css")].id}"', page)

        # An asset belongs to the pages that reference it: publish only the excluded folder and its picture goes up.
        private = self.build(include=["Notes/Areas/Private"], exclude=[])
        self.assertEqual(set(self.assets(private)), {self.asset_name(attachments / "private-only.png")})

    def test_a_linked_file_outside_the_asset_types_is_never_uploaded(self) -> None:
        # A script or stylesheet is an asset; a map or an archive beside it is not, by type as well as by reference.
        write(self.elsewhere / "app.js.map", "{}")
        write(self.elsewhere / "bundle.zip", b"zip")
        write(self.elsewhere / "linked.html", '<script src="app.js.map"></script><a href="bundle.zip">z</a>')
        os.symlink(self.elsewhere / "linked.html", self.art / "Project" / "linked.html")
        built = self.build()
        names = " ".join(self.assets(built))
        self.assertNotIn("app.js.map", names)
        self.assertNotIn("bundle.zip", names)
        linked = self.html(built, "Artifacts/Project/linked.html")
        self.assertIn('src="#onyx-missing"', linked)
        self.assertIn('href="#onyx-unpublished"', linked)

    def test_mirror_assets_stay_inside_home(self) -> None:
        home = self.base / "home"
        outside, sibling = self.base / "outside", self.base / "home-evil"  # the sibling shares home's name as a prefix
        write(home / "notes" / "Docs" / "inside.png", b"png:inside")
        write(outside / "pic.png", b"png:outside")
        write(sibling / "pic.png", b"png:sibling")
        write(outside / "style.css", "body{}")
        os.symlink(outside / "pic.png", home / "notes" / "Docs" / "link.png")  # a link out of home
        write(
            home / "notes" / "Docs" / "Note.md",
            f"# Note\n\n![a](inside.png)\n\n![b](../../../outside/pic.png)\n\n![c]({sibling}/pic.png)\n\n"
            f"![d](link.png)\n\n![e]({outside}/pic.png)\n",
        )
        (home / "art" / "Proj").mkdir(parents=True)
        write(outside / "page.html", '<title>P</title><link rel="stylesheet" href="style.css"><img src="pic.png">')
        os.symlink(outside / "page.html", home / "art" / "Proj" / "page.html")  # the page is named by the include, not floored

        def build(**kwargs):
            return build_mirror(
                roots={"Notes": home / "notes", "Artifacts": home / "art"},
                include=["Notes", "Artifacts"],
                exclude=[],
                ids=fake_ids,
                **kwargs,
            )

        built = build(home=home)
        self.assertEqual(set(self.assets(built)), {self.asset_name(home / "notes" / "Docs" / "inside.png")})
        note = self.html(built, "Notes/Docs/Note.md")
        self.assertEqual(note.count('src="#onyx-missing"'), 4)  # the two paths out, the sibling, and the link out
        self.assertEqual(note.count(f'src="{fake_ids(self.asset_name(home / "notes" / "Docs" / "inside.png"))}"'), 1)
        self.assertNotIn(str(outside), note)
        # The page outside home still publishes, but everything it loads from beside it does not.
        page = self.html(built, "Artifacts/Proj/page.html")
        self.assertEqual(page.count("#onyx-missing"), 2)
        self.assertNotIn("outside", " ".join(self.assets(built)))

        # Widen home to hold them and they go up, so it is the floor keeping them out and nothing else.
        widened = build(home=self.base)
        self.assertEqual(
            {Path(name.removeprefix("asset:")).name for name in self.assets(widened)},
            {"inside.png", "pic.png", "style.css"},
        )

        # With no home given it is the user's own: the temp folder is not inside it, so nothing goes up.
        with mock.patch.object(Path, "home", return_value=self.base / "elsewhere-home"):
            self.assertEqual(self.assets(build()), {})
        with mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(len(self.assets(build())), 1)

    # MARK: links

    def test_mirror_links_point_at_ids_or_unpublished(self) -> None:
        built = self.build()
        beta, gamma = (fake_ids(f"page:Notes/Areas/{name}.md") for name in ("Beta", "Gamma"))
        other = fake_ids("page:Artifacts/Project/other.html")
        alpha = self.html(built, "Notes/Areas/Alpha.md")

        self.assertIn(f'href="{beta}"', alpha)  # [[Beta]]
        self.assertIn(f'href="{beta}#top"', alpha)  # a relative link keeps its fragment
        self.assertIn(f'href="{gamma}#Part%20One"', alpha)  # [[Gamma#Part One]]
        self.assertIn(f'href="{other}"', alpha)  # a link across vaults, to a real file another page stands for
        # The excluded note, four ways: two wikilinks, a relative link, and a file the mirror has no page for.
        self.assertEqual(alpha.count('href="#onyx-unpublished"'), 4)
        self.assertNotIn("#onyx-unpublished#", alpha)
        # Its tooltip would have named the note's path in the vault.
        self.assertNotIn("Private", alpha)
        self.assertNotIn("Secret%20Plan", alpha)
        # Outside addresses are not the mirror's to touch.
        self.assertIn('href="https://example.com/x?a=1&amp;b=2"', alpha)
        self.assertIn('href="mailto:a@example.com"', alpha)
        self.assertIn(f'href="{fake_ids("page:Notes/Areas/Alpha.md")}"', self.html(built, "Notes/Areas/Beta.md"))

        page = self.html(built, "Artifacts/Project/page.html")
        self.assertIn(f'<a href="{other}#sec">', page)
        self.assertIn('<a href="#onyx-unpublished">hidden</a>', page)  # a page the Artifacts folder has no link to
        self.assertIn('<a href="#onyx-unpublished">data</a>', page)
        self.assertIn('<a href="https://example.com">', page)

        guide = self.html(built, "Artifacts/Guides/g1")
        self.assertIn(f'href="{other}"', guide)  # relative, through a link in Artifacts to the page's real file
        self.assertIn('href="#onyx-unpublished"', guide)  # its index.inline.html twin is not listed, so not published

        # Nothing a page links to or loads is a path on this Mac, or a reader URL for one.
        ids = {obj.id for obj in built}
        for path, obj in self.pages(built).items():
            markup = build._RAW_TEXT_RE.sub(lambda m: m.group(1) + m.group(4), obj.data.decode("utf-8"))
            self.assertNotIn("/view?", markup, path)
            self.assertNotIn(str(self.base), markup, path)
            for ref in re.findall(r'<a\b[^>]*?\bhref="([^"]*)"', markup):
                self.assertTrue(
                    ref.split("#")[0] in ids or ref.startswith(("#onyx-", "https://", "mailto:")), f"{path}: {ref}"
                )
            for ref in re.findall(r'<(?:img|link|audio|script)\b[^>]*?\b(?:src|href)="([^"]*)"', markup):
                self.assertTrue(ref in ids or ref.startswith(("#onyx-", "https://")), f"{path}: {ref}")

    def test_a_page_that_failed_is_not_linked_to(self) -> None:
        real_read = viewer.read_local

        def flaky(path):
            if path.name == "Beta.md":
                raise viewer.ViewerError("unreadable")
            return real_read(path)

        with mock.patch.object(viewer, "read_local", flaky):
            built = self.build()
        self.assertEqual(built.skipped, ["Notes/Areas/Beta.md"])
        self.assertNotIn("Notes/Areas/Beta.md", self.pages(built))
        self.assertNotIn(fake_ids("page:Notes/Areas/Beta.md"), self.html(built, "Notes/Areas/Alpha.md"))

    # MARK: determinism

    def test_mirror_build_is_deterministic(self) -> None:
        first = self.build()
        self.assertEqual(self.snapshot(first), self.snapshot(self.build()))
        # The order arguments arrive in is not the order objects leave in.
        reordered = build_mirror(
            roots={"Artifacts": self.art, "Notes": self.notes},
            include=list(reversed(INCLUDE)),
            exclude=list(reversed(EXCLUDE)),
            ids=fake_ids,
            home=self.base,
        )
        self.assertEqual(self.snapshot(first), self.snapshot(reordered))
        names = [obj.name for obj in first]
        pages = [name for name in names if name.startswith("page:")]
        assets = [name for name in names if name.startswith("asset:")]
        self.assertEqual(names, sorted(pages) + sorted(assets))
        # A file no published page reads is nothing to the build; one that changed changes only its own page.
        (self.notes / "Other" / "Attachments" / "unused.png").touch()
        self.assertEqual(self.snapshot(first), self.snapshot(self.build()))
        write(self.notes / "Areas" / "Gamma.md", "# Gamma\n\nchanged\n")
        before, after = {obj.name: obj for obj in first}, {obj.name: obj for obj in self.build()}
        self.assertEqual({name for name in after if after[name].data != before[name].data}, {"page:Notes/Areas/Gamma.md"})
        # Gamma no longer embeds gamma-only.png, so nothing references it and it is no longer uploaded.
        self.assertEqual(set(before) - set(after), {self.asset_name(self.notes / "Other" / "Attachments" / "gamma-only.png")})

    # MARK: Artifacts pages

    def test_mirror_keeps_artifact_scripts_and_drops_base(self) -> None:
        built = self.build()
        page = self.html(built, "Artifacts/Project/page.html")
        self.assertNotIn("<base", page)
        self.assertIn("<title>The Page</title>", page)
        # Scripts stay, as trusted local HTML's do, and a string the script builds is its own: not a reference.
        self.assertIn("""const tpl = '<a href="' + url + '">x</a>'; const img = `<img src="${src}">`; document.title = 'kept';""", page)
        self.assertIn("""a::after{content:"<a href='x'>"}""", page)
        app_js = self.assets(built)[self.asset_name(self.elsewhere / "app.js")]
        self.assertIn(f'<script src="{app_js.id}"></script>', page)
        # The widget is the reader's, not the mirror's.
        for widget in ("ask.js", "askw-src", "askw-folder", "askw-doc-token"):
            self.assertNotIn(widget, page)
        self.assertEqual(self.pages(built)["Artifacts/Project/page.html"].mime, "text/html; charset=utf-8")

    # MARK: what a page carries to the index

    def test_each_page_carries_its_index_entry(self) -> None:
        built = self.build()
        pages = self.pages(built)
        alpha = pages["Notes/Areas/Alpha.md"].page
        self.assertEqual(set(alpha), {"path", "title", "kind", "mtime", "text"})
        self.assertEqual((alpha["path"], alpha["title"], alpha["kind"]), ("Notes/Areas/Alpha.md", "Alpha", "markdown"))
        self.assertEqual(alpha["mtime"], os.stat(self.notes / "Areas" / "Alpha.md").st_mtime)
        self.assertIn("Wikilinks: Beta, Gamma#Part One, Secret Plan", alpha["text"])  # the link text, none of the markup
        self.assertNotIn("<", alpha["text"])
        self.assertNotIn("font-family", alpha["text"])  # the reading shell's stylesheet is not text
        self.assertNotIn("Alpha Alpha", alpha["text"])  # the <title> in the head is dropped; the heading counts once

        self.assertEqual(pages["Notes/Areas/Plain.txt"].page["kind"], "text")
        self.assertEqual(pages["Notes/Areas/Plain.txt"].page["text"], "Plain line one <b>")  # entities undone

        page = pages["Artifacts/Project/page.html"].page
        self.assertEqual((page["title"], page["kind"]), ("The Page", "html"))
        self.assertEqual(page["mtime"], os.stat(self.elsewhere / "page.html").st_mtime)  # the real file, not the link
        self.assertIn("Hello & welcome", page["text"])
        for dropped in ("kept", "const tpl", "The Page", "a::after"):
            self.assertNotIn(dropped, page["text"])
        # The sidebar's label: a <title>, else the guide's folder.
        self.assertEqual(pages["Artifacts/Guides/g1"].page["title"], "Guide One")
        self.assertEqual(pages["Artifacts/Guides/g2"].page["title"], "g2")

        write(self.notes / "Areas" / "Long.md", "# Long\n\n" + "word " * 10_000)
        self.assertEqual(len(self.pages(self.build())["Notes/Areas/Long.md"].page["text"]), 20_000)

    def test_markdown_theme_css_reaches_the_pages_the_reader_themes(self) -> None:
        css = "body{color:rgb(1,2,3)}"
        built = self.build(markdown_css=css)
        themed = f'<style id="askw-markdown-theme">{css}</style></head>'
        self.assertIn(themed, self.html(built, "Notes/Areas/Alpha.md"))
        self.assertIn(themed, self.html(built, "Notes/Areas/Plain.txt"))
        # An authored page keeps its own look, as /view keeps it.
        self.assertNotIn("askw-markdown-theme", self.html(built, "Artifacts/Project/page.html"))
        self.assertNotIn("askw-markdown-theme", self.html(self.build(), "Notes/Areas/Alpha.md"))
        # A theme change is a change to the page's bytes, which is how the publisher knows to send it again.
        self.assertNotEqual(self.pages(built)["Notes/Areas/Alpha.md"].data, self.pages(self.build())["Notes/Areas/Alpha.md"].data)

    def test_markdown_theme_css_is_what_view_injects(self) -> None:
        snapshot = {"mode": "dark", "styles": {"content": {"color": "rgb(196, 197, 181)", "background-color": "rgb(26, 26, 26)"}}}
        storage = Storage(self.base / "data")
        self.addCleanup(storage.close)
        self.assertIsNone(markdown_theme_css(storage, self.notes))  # nothing stored
        self.assertIsNone(markdown_theme_css(storage, None))
        storage.save_markdown_theme(self.notes, snapshot)
        css = markdown_theme_css(storage, self.notes)
        self.assertEqual(css, build.markdown_theme.stylesheet(snapshot))
        self.assertIn("rgb(196, 197, 181)", css)
        storage.update_settings({"markdown_follow_obsidian": False}, model_default="sonnet")
        self.assertIsNone(markdown_theme_css(storage, self.notes))  # "Match Obsidian" is off, so /view injects nothing

    def test_with_both_modes_measured_a_page_carries_both_for_the_phone_to_pick(self) -> None:
        dark = {"mode": "dark", "styles": {"content": {"color": "rgb(196, 197, 181)", "background-color": "rgb(26, 26, 26)"}}}
        light = {"mode": "light", "styles": {"content": {"color": "rgb(0, 43, 54)", "background-color": "rgb(253, 246, 227)"}}}
        storage = Storage(self.base / "data")
        self.addCleanup(storage.close)
        storage.save_markdown_theme(self.notes, dark, light)
        css = markdown_theme_css(storage, self.notes)
        stylesheet = build.markdown_theme.stylesheet
        self.assertEqual(css, f"@media (prefers-color-scheme: light) {{\n{stylesheet(light)}\n}}\n"
                              f"@media (prefers-color-scheme: dark) {{\n{stylesheet(dark)}\n}}")
        # The Mac's Color theme doesn't narrow it: the phone has its own Appearance setting.
        storage.update_settings({"vault_mode": "dark"}, model_default="sonnet")
        self.assertEqual(markdown_theme_css(storage, self.notes), css)

    # MARK: a bad file

    def test_one_bad_file_is_skipped_and_the_rest_build(self) -> None:
        real_load = viewer.load_local_document

        def flaky(path, **kwargs):
            if path.name == "Gamma.md":
                raise viewer.ViewerError("boom")
            if path.name == "Keep.md":
                raise RuntimeError("anything at all")
            return real_load(path, **kwargs)

        with mock.patch.object(viewer, "load_local_document", flaky):
            built = self.build()
        self.assertEqual(sorted(built.skipped), ["Notes/Areas/Gamma.md", "Notes/Areas/Private Extras/Keep.md"])
        self.assertNotIn("Notes/Areas/Gamma.md", self.pages(built))
        self.assertIn("Notes/Areas/Alpha.md", self.pages(built))
        self.assertIn("Artifacts/Guides/g1", self.pages(built))
        # A page that isn't published uploads nothing.
        self.assertNotIn("gamma-only.png", " ".join(self.assets(built)))
        self.assertEqual(self.build().skipped, [])


if __name__ == "__main__":
    unittest.main()
