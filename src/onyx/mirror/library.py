"""The phone's Library home: recently opened pages and recent asks (wire format, "Library").

Both lists come from Onyx's own history, which keys a document by its realpath, so each entry is mapped to the page
the mirror published for that file (``MirrorBuild.pages_by_real``). Anything that maps to no published page is left
out: the phone never learns that a file outside the mirror was opened or asked about.

A thread of asks (a question and its follow-ups) becomes a page of its own that draws as the Mac draws a saved answer
in Recent conversations (``panels_ui``): the same cards (meta line, badges, Passage / Question / Answer), the same CSS,
in the app's tokens — the vault look the Mac wears, else the app's own palette — and each answer drawn on the phone by
the widget's own Markdown renderer (``panels_ui.answer_markdown``), so the two can't drift. The answer travels as
escaped text inside its card: without the script it reads as plain text, and the renderer escapes before it formats.
"""

from __future__ import annotations

import html as _html
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import launcher_ui, panels_ui
from .build import PAGE_MIME, BuiltObject, _plain_text

LIBRARY_VERSION = 1
MAX_RECENT = 30
MAX_THREADS = 100
MAX_TITLE_CHARS = 120
CHATS = "Chats"
LABELS = {"ask": "Ask", "eli5": "Explain like I'm 5", "prove": "Prove it"}
# The Mac's own words for a saved answer's badges (panels_ui: actionLabel, modeLabel).
ACTION_BADGES = {"ask": "Question", "eli5": "ELI5", "prove": "Prove it"}
MODE_BADGES = {"generated": "Generated", "rerun": "Asked again", "edited": "Edited & asked", "continue": "Continued"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# The rules Recent conversations draws a saved answer with, taken from panels_ui rather than copied, so a change there
# reaches the phone's copy on the next publish.
_CARD_RULES = (".badges", ".badge", ".hist-block", ".hist-answer", ".hist-actions")


@dataclass(frozen=True)
class LibraryBuild:
    pages: list[BuiltObject]  # one page per thread of asks
    library: dict[str, Any]  # the `library` object's JSON


def _page_for(source: Any, by_real: dict[str, str]) -> str | None:
    if not isinstance(source, str) or not source.startswith("/"):
        return None  # a remote page or a pasted selection: nothing on the phone to open
    return by_real.get(os.path.realpath(source))


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _title(first: dict[str, Any]) -> str:
    question = (first.get("question") or "").strip()
    if question:
        return _clip(question, MAX_TITLE_CHARS)
    label = LABELS.get(first.get("action") or "", "Ask")
    return _clip(f"{label}: {first.get('selection') or ''}", MAX_TITLE_CHARS)


def _card_css() -> str:
    lines = [line for line in panels_ui.PANELS_CSS.splitlines() if line.startswith(_CARD_RULES)]
    # The Mac's card is a modal pane; on the phone it is the page, so it runs to its full height, at reading size.
    phone = (
        ".hist-block>pre,.hist-answer{max-height:none;font-size:15px;line-height:1.55}"
        ".hist-answer :is(h1,h2,h3,h4,h5,h6){font-size:16px} .hist-answer code,.hist-answer table{font-size:13.5px}"
        ".badge{font-size:10px} .hist-block>h4{font-size:11px}"
    )
    return "\n".join(lines) + "\n" + phone


def _tokens_css(look: dict[str, Any] | None) -> str:
    # The app's own palette, light and dark (launcher_ui.theme_style's token rules), then the vault's over it while the
    # Mac wears the vault look; the phone holds its web view to that look's mode, as the Mac's window is held.
    base = "\n".join(launcher_ui.theme_style(None).splitlines()[:3])
    if not look:
        return base
    decls = ";".join(f"{name}:{value}" for name, value in look["tokens"].items())
    return f"{base}\n:root{{{decls};color-scheme:{look['mode']}}}"


def _when(epoch: float) -> str:
    # The Mac's line is `new Date(…).toLocaleString()`, which in an en-US locale reads like this.
    return time.strftime("%-m/%-d/%Y, %-I:%M:%S %p", time.localtime(epoch))


def _meta(turn: dict[str, Any]) -> str:
    # The Mac's line: when · provider · model · effort · how long it took.
    parts = [_when(float(turn["started_at"])), turn.get("provider") or "claude", turn.get("model") or ""]
    if turn.get("effort"):
        parts.append(turn["effort"])
    if turn.get("latency_ms"):
        parts.append(f"{int(turn['latency_ms']) / 1000:.1f}s")
    return " · ".join(filter(None, parts))


def _card(turn: dict[str, Any], *, passage: str | None) -> str:
    esc = _html.escape
    action = turn.get("action") or "ask"
    mode = turn.get("request_mode") or "generated"
    question = (turn.get("question") or "").strip() or ACTION_BADGES.get(action, action)
    blocks = [
        f'<p class="meta">{esc(_meta(turn))}</p>',
        f'<div class="badges"><span class="badge {esc(mode)}">{esc(MODE_BADGES.get(mode, "Generated"))}</span>'
        f'<span class="badge">{esc(ACTION_BADGES.get(action, action))}</span></div>',
    ]
    if passage is not None:
        blocks.append(f'<div class="hist-block"><h4>Passage</h4><pre>{esc(passage or "No saved passage")}</pre></div>')
    blocks.append(f'<div class="hist-block"><h4>Question</h4><pre>{esc(question)}</pre></div>')
    # Escaped text in a plain card: the script below swaps in the rendered answer, and without it this still reads.
    blocks.append(f'<div class="hist-block"><h4>Answer</h4><div class="hist-answer plain" data-md>'
                  f'{esc(turn.get("answer") or "No answer")}</div></div>')
    return '<article class="turn">' + "".join(blocks) + "</article>"


def _thread_html(turns: list[dict[str, Any]], doc_id: str, doc_title: str, look: dict[str, Any] | None) -> str:
    esc = _html.escape
    first_passage = (turns[0].get("selection") or "").strip()
    cards = []
    for number, turn in enumerate(turns):
        passage = (turn.get("selection") or "").strip()
        # Every card of a follow-up shares its first passage; the Mac shows each saved answer alone, so on one page the
        # repeat is dropped, and a follow-up on a different passage keeps its own.
        cards.append(_card(turn, passage=passage if number == 0 or passage != first_passage else None))
    actions = f'<div class="hist-actions"><a class="secondary" href="{doc_id}">Open document</a></div>'
    renderer = panels_ui.answer_markdown()
    script = (
        "<script>(function(){var md=" + renderer + ";"
        "document.querySelectorAll('.hist-answer[data-md]').forEach(function(el){"
        "if(!md||!el.textContent)return;el.innerHTML=md(el.textContent);el.classList.remove('plain')});})();</script>"
    )
    style = (
        _tokens_css(look) + "\n" + _card_css() + "\n"
        "*{box-sizing:border-box} html{background:rgb(var(--bg-primary))}"
        "body{margin:0;padding:20px 18px 48px;background:rgb(var(--bg-primary));color:rgb(var(--ink));"
        "font:15px/1.5 var(--ui-font);-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%}"
        ".meta{margin:0;color:rgb(var(--muted));font-size:13px} .turn+.turn{margin-top:22px;padding-top:22px;"
        "border-top:1px solid var(--line-soft)}"
        "a.secondary{display:inline-flex;padding:8px 14px;border:1px solid var(--line-soft);border-radius:8px;"
        "background:rgb(var(--ink)/.055);color:rgb(var(--ink));font-weight:500;text-decoration:none}"
    )
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(doc_title or 'Saved answer')}</title><style>{style}</style></head>"
        f"<body>{''.join(cards)}{actions}{script}</body></html>"
    )


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


def build_library(storage: Any, *, built: Any, ids: Callable[[str], str], look: dict[str, Any] | None,
                  chats: bool) -> LibraryBuild:
    """The Library's lists and the thread pages, for what ``built`` (a ``MirrorBuild``) published.

    ``look`` is the vault palette the Mac wears (the index's ``look.vault``), or None for the app's own palette.
    """
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
            if turns[-1].get("recent_hidden"):
                continue  # removed from Recents on the Mac (asking on in the thread brings it back)
            first = turns[0]
            doc = _page_for(first.get("document_source"), by_real)
            if doc is None:
                continue  # asked on something the mirror doesn't publish, so its answers stay on the Mac
            rid = first["request_id"]
            key = rid if isinstance(rid, str) and _SAFE_ID.match(rid) else ids(f"request:{rid}")[:32]
            path = f"{CHATS}/{key}"
            name = f"page:{path}"
            title = _title(first)
            markup = _thread_html(turns, doc, titles.get(doc) or first.get("document_title") or "", look)
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

