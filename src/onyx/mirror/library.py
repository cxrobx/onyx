"""The phone's Library home: recently opened pages and recent asks (wire format, "Library").

Both lists come from Onyx's own history, which keys a document by its realpath, so each entry is mapped to the page
the mirror published for that file (``MirrorBuild.pages_by_real``). Anything that maps to no published page is left
out: the phone never learns that a file outside the mirror was opened or asked about.

A thread of asks (a question and its follow-ups) becomes a page of its own, built here in the reading shell and the
vault's reading theme, so the phone reads it like any note. Its answers are Markdown the model wrote, rendered with raw
HTML off; a link in one survives only for http(s), because a local path in an answer means nothing on the phone.
"""

from __future__ import annotations

import html as _html
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from markdown_it import MarkdownIt

from .. import viewer
from .build import PAGE_MIME, BuiltObject, _plain_text

LIBRARY_VERSION = 1
MAX_RECENT = 30
MAX_THREADS = 100
MAX_SELECTION_CHARS = 1500
MAX_TITLE_CHARS = 120
CHATS = "Chats"
LABELS = {"ask": "Ask", "eli5": "Explain like I'm 5", "prove": "Prove it"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_HREF = re.compile(r'(<a href=")([^"]*)(")')
_IMG_SRC = re.compile(r'(<img src=")([^"]*)(")')


def _answer_markdown() -> MarkdownIt:
    # CommonMark with raw HTML escaped, as notes are (viewer._build_markdown), minus the vault's wikilinks: an answer
    # names no vault note by [[link]], and those rules need a document to resolve against.
    return MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
        ["table", "strikethrough"]
    )


_MD = _answer_markdown()


@dataclass(frozen=True)
class LibraryBuild:
    pages: list[BuiltObject]  # one page per thread of asks
    library: dict[str, Any]  # the `library` object's JSON


def _page_for(source: Any, by_real: dict[str, str]) -> str | None:
    if not isinstance(source, str) or not source.startswith("/"):
        return None  # a remote page or a pasted selection: nothing on the phone to open
    return by_real.get(os.path.realpath(source))


def _render_answer(markdown: str) -> str:
    rendered = _MD.render(markdown or "")
    rendered = _HREF.sub(
        lambda m: m.group(1) + (m.group(2) if _html.unescape(m.group(2)).lower().startswith(("http://", "https://"))
                                else "#onyx-unpublished") + m.group(3),
        rendered,
    )
    # The reader's CSP blocks remote images anyway; an empty source keeps a local path from even being tried.
    return _IMG_SRC.sub(lambda m: m.group(1) + m.group(3), rendered)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _title(first: dict[str, Any]) -> str:
    question = (first.get("question") or "").strip()
    if question:
        return _clip(question, MAX_TITLE_CHARS)
    label = LABELS.get(first.get("action") or "", "Ask")
    return _clip(f"{label}: {first.get('selection') or ''}", MAX_TITLE_CHARS)


def _when(epoch: float) -> str:
    return time.strftime("%b %-d, %Y at %-I:%M %p", time.localtime(epoch))


def _thread_html(title: str, turns: list[dict[str, Any]], doc_id: str, doc_title: str) -> str:
    esc = _html.escape
    parts = [f'<p class="onyx-chat-source"><a href="{doc_id}">{esc(doc_title)}</a></p>', f"<h1>{esc(title)}</h1>"]
    for number, turn in enumerate(turns):
        label = LABELS.get(turn.get("action") or "", "Ask") if number == 0 else "Follow-up"
        meta = " · ".join(filter(None, [label, turn.get("model") or "", _when(float(turn["started_at"]))]))
        section = [f'<p class="onyx-chat-meta"><small>{esc(meta)}</small></p>']
        selection = (turn.get("selection") or "").strip()
        if number == 0 and selection:
            section.append(f"<blockquote>{esc(_clip(selection, MAX_SELECTION_CHARS))}</blockquote>")
        question = (turn.get("question") or "").strip()
        if question and not (number == 0 and question == title):
            section.append(f'<p class="onyx-chat-q"><strong>{esc(question)}</strong></p>')
        section.append(_render_answer(turn.get("answer") or ""))
        parts.append('<section class="onyx-chat-turn">' + "".join(section) + "</section>" + ("<hr>" if number < len(turns) - 1 else ""))
    return viewer._reading_shell(title, "".join(parts), kind="markdown")


def _threads(conversations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Completed asks grouped under the first ask of their thread, each thread oldest first."""
    by_id = {c["request_id"]: c for c in conversations}

    def root(conv: dict[str, Any]) -> str:
        seen, current = set(), conv
        while (parent := current.get("parent_request_id")) and parent in by_id and parent not in seen:
            seen.add(current["request_id"])
            current = by_id[parent]
        return current["request_id"]

    groups: dict[str, list[dict[str, Any]]] = {}
    for conv in conversations:
        if conv.get("status") == "complete":
            groups.setdefault(root(conv), []).append(conv)
    threads = [sorted(turns, key=lambda c: float(c["started_at"])) for turns in groups.values()]
    return sorted(threads, key=lambda turns: -float(turns[-1]["started_at"]))


def build_library(storage: Any, *, built: Any, ids: Callable[[str], str], markdown_css: str | None,
                  chats: bool) -> LibraryBuild:
    """The Library's lists and the thread pages, for what ``built`` (a ``MirrorBuild``) published."""
    by_real: dict[str, str] = getattr(built, "pages_by_real", {}) or {}
    titles = {obj.id: obj.page["title"] for obj in built if obj.page is not None}

    recent: list[dict[str, Any]] = []
    listed: set[str] = set()
    for doc in storage.recent_documents(100):
        page = _page_for(doc.get("source"), by_real)
        if page is None or page in listed:
            continue
        listed.add(page)
        recent.append({"id": page, "opened_at": float(doc["last_opened_at"])})
        if len(recent) == MAX_RECENT:
            break

    pages: list[BuiltObject] = []
    threads: list[dict[str, Any]] = []
    if chats:
        for turns in _threads(storage.recent_conversations(limit=200)):
            first = turns[0]
            doc = _page_for(first.get("document_source"), by_real)
            if doc is None:
                continue  # asked on something the mirror doesn't publish, so its answers stay on the Mac
            rid = first["request_id"]
            key = rid if isinstance(rid, str) and _SAFE_ID.match(rid) else ids(f"request:{rid}")[:32]
            path = f"{CHATS}/{key}"
            name = f"page:{path}"
            title = _title(first)
            markup = _thread_html(title, turns, doc, titles.get(doc) or first.get("document_title") or "")
            if markdown_css:
                markup = markup.replace("</head>", f'<style id="askw-markdown-theme">{markdown_css}</style></head>', 1)
            updated = float(turns[-1]["started_at"])
            page_id = ids(name)
            pages.append(BuiltObject(name=name, id=page_id, data=markup.encode("utf-8"), mime=PAGE_MIME, page={
                "path": path, "title": title, "kind": "chat", "mtime": updated, "text": _plain_text(markup),
            }))
            threads.append({"id": page_id, "doc": doc, "title": title, "action": first.get("action") or "ask",
                            "turns": len(turns), "started_at": float(first["started_at"]), "updated_at": updated})
            if len(threads) == MAX_THREADS:
                break

    return LibraryBuild(pages=pages, library={"v": LIBRARY_VERSION, "recent": recent, "chats": threads})
