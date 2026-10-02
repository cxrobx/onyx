"""Keep a notes vault's links pointing where they did when Onyx moves or renames something in it.

Obsidian rewrites links itself ("Automatically update internal links") only for a move made in Obsidian; one made
anywhere else reaches it as a delete and a create, and every link to the note is left broken. So a move from Onyx's
sidebar does the rewriting, the way Obsidian would:

- ``[text](../Folder/Note.md)`` and ``![](pic.png)`` written relative to the note (or to the vault) are recomputed
  from where the note and its target now are, in the same style and encoding;
- ``[[Note]]``, ``![[Note]]`` and ``[[Folder/Note#Heading|Alias]]`` that would now resolve somewhere else — a path
  that moved, a name that changed, a name that became ambiguous — are rewritten to the shortest form that resolves to
  the target, keeping the heading and the alias.

The plan is made before anything moves, from the index as it is and a copy of it as it will be; each note is then
written through ``viewer.write_source`` against the version the plan read, so an edit made meanwhile is never
overwritten — that note is reported, not saved. Code blocks and code spans are left alone, and a note inside a
linked folder is another tree's file, so it is reported rather than written (vault mutations stay in the vault).
"""

from __future__ import annotations

import bisect
import os
import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path

from markdown_it import MarkdownIt

from . import viewer
from .vault import VaultFile, VaultIndex

MARKDOWN_EXTENSIONS = {".md", ".markdown"}

_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_MD_LINK_RE = re.compile(
    r"(?<!\\)(!?)\[((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\("
    r"[ \t]*(<[^<>\n]*>|[^\s()<>]*(?:\([^\s()]*\)[^\s()<>]*)*)"
    r"((?:[ \t]+\"[^\"\n]*\"|[ \t]+'[^'\n]*')?)[ \t]*\)"
)
# A code span may wrap a line but never a paragraph, so a stray backtick can't hide the links of the paragraphs after it.
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)\1(?!`)", re.DOTALL)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_FENCES = MarkdownIt("commonmark")


@dataclass
class Rewrite:
    rel: str  # the note's vault path once the move is made
    sig: str  # the version the plan read and rewrote
    text: str
    links: int


@dataclass
class Plan:
    rewrites: list[Rewrite] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # notes that needed a change but sit in a linked folder
    unreadable: list[str] = field(default_factory=list)  # notes that link to it but can't be edited (not UTF-8, too big)


def file_moves(index: VaultIndex, entry: Path, moved: Path) -> dict[str, str]:
    """The vault path of every indexed file at or beneath ``entry`` → the one it has once ``entry`` is ``moved``."""
    old = entry.relative_to(index.root).as_posix()
    new = moved.relative_to(index.root).as_posix()
    out: dict[str, str] = {}
    for item in index.files:
        if item.rel == old:
            out[item.rel] = new
        elif item.rel.startswith(old + "/"):
            out[item.rel] = new + item.rel[len(old):]
    return out


def moved_index(index: VaultIndex, moves: dict[str, str]) -> VaultIndex:
    """The index as it will be once ``moves`` are made: the same files, some at new paths."""
    files: list[VaultFile] = []
    for item in index.files:
        rel = moves.get(item.rel)
        if rel is None:
            files.append(item)
            continue
        name = rel.rsplit("/", 1)[-1]
        files.append(replace(item, path=index.root / rel, rel=rel, name=name, stem_key=os.path.splitext(name)[0].casefold()))
    after = VaultIndex(root=index.root, kind=index.kind, files=files, attachment_folder=index.attachment_folder)
    for item in after.files:
        after._by_rel.setdefault(item.rel.casefold(), item)
        after._by_stem.setdefault(item.stem_key, []).append(item)
        after._by_name.setdefault(item.name.casefold(), []).append(item)
    return after


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Where the text is code — fenced and indented blocks, and code spans — as sorted (start, end) offsets."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    ranges: list[tuple[int, int]] = []
    for token in _FENCES.parse(text):
        if token.type in ("fence", "code_block") and token.map:
            a, b = token.map
            ranges.append((starts[a], starts[min(b, len(starts) - 1)]))
    blocks = list(ranges)
    for found in _CODE_SPAN_RE.finditer(text):
        if not any(a <= found.start() < b for a, b in blocks):
            ranges.append(found.span())
    ranges.sort()
    return ranges


def _in(ranges: list[tuple[int, int]], starts: list[int], pos: int) -> bool:
    i = bisect.bisect_right(starts, pos) - 1
    return i >= 0 and ranges[i][0] <= pos < ranges[i][1]


def _wikilink_text(after: VaultIndex, target: VaultFile, source: Path, *, embed: bool, written: str) -> str | None:
    """The shortest ``[[…]]`` target that resolves to ``target`` from ``source``: its name if that is unambiguous, else
    its path in the vault. An extension the link was written with is kept; a note's ``.md`` is left off otherwise."""
    resolve = after.resolve_embed if embed else after.resolve_wikilink
    keep_ext = target.path.suffix.lower() not in MARKDOWN_EXTENSIONS or written.casefold().endswith(target.path.suffix.lower())
    name = target.name if keep_ext else os.path.splitext(target.name)[0]
    path = target.rel if keep_ext else os.path.splitext(target.rel)[0]
    for candidate in (name, path):
        found = resolve(candidate, source=source)
        if found is not None and found.rel == target.rel:
            return candidate
    return None


def _encode_like(original: str, new: str) -> str:
    """``new`` written as ``original`` was: percent-encoded if it was (or if it must be, having a space)."""
    if urllib.parse.unquote(original) != original or " " in new:
        return urllib.parse.quote(new, safe="/!$&'()*+,;=:@-._~")
    return new


def rewrite_note(
    text: str, before: VaultIndex, after: VaultIndex, moves: dict[str, str], old_rel: str, new_rel: str
) -> tuple[str, int]:
    """``text`` (the note at ``old_rel``, at ``new_rel`` once the move is made) with each link that the move would break
    rewritten; and how many were."""
    old_src, new_src = before.root / old_rel, after.root / new_rel
    code = _code_ranges(text)
    code_starts = [a for a, _ in code]
    edits: list[tuple[int, int, str]] = []

    for found in _WIKILINK_RE.finditer(text):
        if _in(code, code_starts, found.start()):
            continue
        embed, inner = found.group(1) == "!", found.group(2)
        target, bar, alias = inner.partition("|")
        escaped = target.endswith("\\")  # `[[Note\|alias]]` in a table
        if escaped:
            target = target[:-1]
        base, hash_, heading = target.partition("#")
        if not base.strip():
            continue  # `[[#Heading]]` names this note
        resolve_before = before.resolve_embed if embed else before.resolve_wikilink
        was = resolve_before(base.strip(), source=old_src)
        if was is None:
            continue  # broken already; nothing to keep pointing
        goal = moves.get(was.rel, was.rel)
        resolve_after = after.resolve_embed if embed else after.resolve_wikilink
        now = resolve_after(base.strip(), source=new_src)
        if now is not None and now.rel == goal:
            continue
        written = _wikilink_text(after, after._by_rel[goal.casefold()], new_src, embed=embed, written=base.strip())
        if written is None:
            continue
        inner_new = written + hash_ + heading + ("\\" if escaped else "") + bar + alias
        edits.append((found.start(2), found.end(2), inner_new))

    for found in _MD_LINK_RE.finditer(text):
        if _in(code, code_starts, found.start()):
            continue
        dest = found.group(3)
        angle = dest.startswith("<")
        raw = dest[1:-1] if angle else dest
        if not raw or raw.startswith(("#", "/")) or _SCHEME_RE.match(raw):
            continue
        path_part, hash_, fragment = raw.partition("#")
        decoded = urllib.parse.unquote(path_part)
        old_dir = posixpath.dirname(old_rel)
        relative = posixpath.normpath(posixpath.join(old_dir, decoded))
        absolute = posixpath.normpath(decoded)
        if not relative.startswith("../") and relative.casefold() in before._by_rel:
            style, was_rel = "relative", before._by_rel[relative.casefold()].rel
        elif not absolute.startswith("../") and absolute.casefold() in before._by_rel:
            style, was_rel = "vault", before._by_rel[absolute.casefold()].rel
        else:
            continue  # not a file in this vault
        goal = moves.get(was_rel, was_rel)
        if style == "relative":
            new_path = posixpath.relpath(goal, posixpath.dirname(new_rel) or ".")
            if decoded.startswith("./") and not new_path.startswith("../"):
                new_path = "./" + new_path
        else:
            new_path = goal
        if goal == was_rel and (style == "vault" or old_rel == new_rel):
            continue  # neither end moved in a way this link sees
        if posixpath.normpath(new_path) == posixpath.normpath(decoded):
            continue  # both moved together

        written = new_path if angle else _encode_like(path_part, new_path)
        new_dest = written + hash_ + fragment
        edits.append((found.start(3), found.end(3), f"<{new_dest}>" if angle else new_dest))

    if not edits:
        return text, 0
    edits.sort()
    out, last = [], 0
    for start, end, new in edits:
        if start < last:
            continue  # overlapping matches: the first one's
        out += [text[last:start], new]
        last = end
    out.append(text[last:])
    return "".join(out), len(edits)


def _in_linked(index: VaultIndex, rel: str) -> bool:
    return any(rel == linked or rel.startswith(linked + "/") for linked in index.symlinked_dirs)


def plan(index: VaultIndex, moves: dict[str, str]) -> Plan:
    """What rewriting a move needs: every note whose links it would break, read now, with its new text."""
    out = Plan()
    if not moves:
        return out
    after = moved_index(index, moves)
    for item in index.files:
        if item.kind != "note" or item.path.suffix.lower() not in MARKDOWN_EXTENSIONS or item.missing:
            continue
        new_rel = moves.get(item.rel, item.rel)
        try:
            text, sig = viewer.read_source(Path(os.path.realpath(item.path)))
        except viewer.ViewerError:
            # Only worth saying when the note mentions what moved; most unreadable notes never link to it.
            try:
                raw = item.path.read_bytes()[: viewer.MAX_EDIT_BYTES * 2].decode("utf-8", "replace")
            except OSError:
                continue
            if any(Path(rel).stem in raw for rel in moves):
                out.unreadable.append(item.rel)
            continue
        rewritten, links = rewrite_note(text, index, after, moves, item.rel, new_rel)
        if not links:
            continue
        if _in_linked(index, item.rel):
            out.skipped.append(item.rel)
            continue
        out.rewrites.append(Rewrite(rel=new_rel, sig=sig, text=rewritten, links=links))
    return out


def apply(root: Path, planned: Plan) -> dict:
    """Write the plan's notes, each against the version it was read at. Run after the move, so ``rel`` is where each
    note now is. A note changed meanwhile, or gone, is reported in ``failed`` and left as it is."""
    updated: list[str] = []
    failed: list[str] = []
    links = 0
    for rewrite in planned.rewrites:
        try:
            viewer.write_source(Path(os.path.realpath(root / rewrite.rel)), rewrite.text, base=rewrite.sig)
        except (viewer.SourceConflict, viewer.ViewerError, OSError):
            failed.append(rewrite.rel)
            continue
        updated.append(rewrite.rel)
        links += rewrite.links
    return {"updated": updated, "links": links, "failed": failed, "skipped": planned.skipped, "unreadable": planned.unreadable}
