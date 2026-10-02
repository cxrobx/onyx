"""Search inside Notes and Artifacts by the words in a page and by what it means: the ⌘P palette's passages, and the
related pane's neighbours.

Onyx keeps no index of its own. It reads the one the vault MCP maintains (vault-mcp's ``data/index.db``; see
``index_path``): every note and artifact cut into passages at its headings, each with an FTS5 row for its words and a
normalised embedding for its meaning. That index already covers both of Onyx's vaults, the Notes vault and Artifacts
mounted beside it as ``Artifacts/``, and every Claude session refreshes it on startup, so a second copy here would
only drift. Onyx opens it read-only and never writes to it.

A search ranks as vault-mcp's ``search_vault`` does: a words leg (BM25, every word present) and a meaning leg (cosine
against the query, embedded by the local Ollama), fused by Reciprocal Rank Fusion. Three things suit a box being typed
into: the last word matches as a prefix, meaning counts only above ``MEANING_FLOOR``, and each page is one row, at its
best passage. The frozen app carries no numpy, so the meaning leg is a plain-Python scan (``math.sumprod``: about
200 ms over 10k passages, in a worker thread), and a newer query stops an older one's scan partway.

``related`` is the same passages read the other way round: instead of a query's direction, the direction of the page
being read, averaged over its own passages, with the nearest other pages around it. Since the page's vectors are
already in the index it embeds nothing and needs no Ollama.

It differs from vault-mcp's ``related_notes`` in one way, and the difference is the whole feature. A bare cosine here
is not a similarity: this model puts a strong common direction into every vector, so two unrelated passages of the
author's vault score 0.635, and the pages lying nearest that direction — the long, topically diffuse ones — come back
as neighbours of everything. Measured, "Tmux and Ghostty" returned meeting notes at 0.85. So the baseline is
estimated once per index (``centroid``) and taken back out of every score (``against_baseline``), which leaves a
number that means "closer than any two pages in this vault are anyway", is comparable between pages, and can
therefore carry a floor. With the baseline out, that same page returns nothing above the floor — which is the truth,
since its subject appears nowhere else in the vault.

It degrades rather than breaks. No index: no passages, and titles still come from ``/api/vault/search``. No Ollama:
words only, and related is unaffected. Every answer says which legs ran and why one didn't, for the palette's footer.
"""

from __future__ import annotations

import array
import heapq
import itertools
import json
import logging
import math
import operator
import os
import re
import sqlite3
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger("onyx.search")

DEFAULT_INDEX = "~/Projects/vault-mcp/data/index.db"  # vault-mcp's own default (its VAULT_MCP_DB)
DEFAULT_CONFIGS = "~/.config/vault-mcp"  # where vault-mcp's config files live, one per index (vault_indexes)
DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"
# What vault-mcp calls the Artifacts folder in its paths: its default mount, VAULT_MCP_MOUNTS="Artifacts=~/Documents/Artifacts".
ARTIFACTS_MOUNT = "Artifacts"
# nomic-embed-text is task-prefixed: passages went in as "search_document: …", so a query goes in as this.
QUERY_PREFIX = "search_query: "
# Ollama drops a model after five idle minutes, and loading it again costs ~0.8 s; keep it for a reading session.
KEEP_ALIVE = "30m"
EMBED_TIMEOUT = 8.0
RRF_K = 60  # vault-mcp's fusion constant (Cormack et al. 2009)
CANDIDATES = 50  # passages drawn from each leg before fusion, as vault-mcp does
MAX_TERMS = 32
MIN_WORDS = 2  # characters typed before the words leg runs
MIN_MEANING = 3  # … and before a query is worth embedding
# Meaning alone must come at least this close. nomic-embed-text's cosines are compressed: on the author's index (10k
# passages) sensible queries top out at 0.65–0.74, while keyboard noise ("asdf", "xqzv flurb") still reaches 0.55–0.56
# with the whole corpus a hair below. Under the floor the meaning leg has nothing to say, so it says nothing.
MEANING_FLOOR = 0.6
RELOAD_EVERY = 5.0  # seconds between asking the index whether it has been rebuilt
SCAN_BLOCK = 1024  # passages scored between looks for a newer query
SNIPPET_LEN = 160
# The related pane scores against the corpus baseline (``_Passages.mu``), not with a bare cosine, so these two are on
# a scale where 0 means "no closer than any two pages in the vault are anyway" and 1 means the same text. Measured
# over 150 pages of the author's vault, a page's nearest neighbour ran 0.29 to 0.94, median 0.61.
#
# Below this a page is not worth putting in front of anyone: it is the tail every page has. It sits at the tenth
# percentile of that sample's best-neighbour scores, which is the line that makes it mean something — a page whose
# *best* neighbour is under it has no neighbour, and is one of the tenth of pages that don't. In the median case a
# page shows 5 rows; a note whose subject appears nowhere else shows none, which is the honest answer and the reason
# there is a floor at all: the pane says so rather than padding itself to twenty.
RELATED_FLOOR = 0.45
# Near enough to be the same content twice, and worth saying so about rather than opening again. A label that tells
# someone two pages are the same has to be right whenever it appears, and over 1731 rows this marks 3.
#
# Measured, the score alone can't carry it: at 0.94 sat two different bass exercises, and the commonest real
# duplicate in the vault — a note beside its own rendering, ``Jev.md`` and ``Jev.html`` — sits lower, at 0.87. So the
# bar for score alone is set where the sample held nothing but true duplicates, and it is a backstop rather than the
# main way in.
DUPE_AT = 0.95
# The main way in is the name, for a pair already reading as one subject. A name is only evidence when it is the
# vault's name for one document rather than its word for a kind of file: ``proposal.typ`` sits in every client folder
# and means nothing, and no blocklist of such words would stay complete, so the index counts them instead and a name
# two pages share is evidence where a name five share is a filing convention.
DUPE_NAMED = 0.60
DUPE_NAME_SHARED_BY = 2
# The related map draws this many of a page's neighbours, and places them by how related they are to one another as
# well as to the page (``related``'s ``near``). Past twelve, the dots and their scores stop fitting a pane 210px wide.
RELATED_MAP = 12
CENTROID_SAMPLE = 2000  # passages the corpus baseline is estimated from; 2000 lands cos=0.9999 on the whole corpus

# A C dot product from 3.12 (the app's Python is 3.14); 3.11, the oldest a checkout runs on, takes the slow road.
_dot: Callable[[Sequence[float], Sequence[float]], float] = getattr(math, "sumprod", None) or (
    lambda a, b: sum(map(operator.mul, a, b))
)
# Ollama is local, so never through a proxy the environment names.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def index_path() -> Path:
    """vault-mcp's index: ``ONYX_VAULT_INDEX``, else where vault-mcp keeps it by default."""
    return Path(os.environ.get("ONYX_VAULT_INDEX") or DEFAULT_INDEX).expanduser()


def ollama_url() -> str:
    return (os.environ.get("ONYX_OLLAMA") or DEFAULT_OLLAMA).rstrip("/")


def vault_indexes(config_dir: Path | str | None = None) -> dict[str, Path]:
    """vault-mcp's other indexes, by the vault folder each covers: ``{folder, lexically normalised: index file}``.

    vault-mcp keeps one index per config file (its ``VAULT_MCP_CONFIG``). ``config.toml`` is the primary vault's, read
    through ``index_path``; a vault indexed on its own has a config beside it naming its ``vault`` and its ``db``
    (``darklabel.toml``). That is how another notes vault in Onyx is searched: by the index already kept for it,
    rather than by mounting it into the primary's, which every Claude session's ``search_vault`` reads too. A config
    without a ``db`` writes the primary index, so it names no index of its own here.
    """
    directory = Path(config_dir or os.environ.get("ONYX_VAULT_MCP_CONFIGS") or DEFAULT_CONFIGS).expanduser()
    found: dict[str, Path] = {}
    try:
        configs = sorted(directory.glob("*.toml"))
    except OSError:
        return found
    for path in configs:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        folder, db = data.get("vault"), data.get("db")
        if isinstance(folder, str) and folder.strip() and isinstance(db, str) and db.strip():
            found.setdefault(os.path.normpath(os.path.expanduser(folder.strip())), Path(db.strip()).expanduser())
    return found


def _short(path: Path) -> str:
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


# MARK: - Words and text

# A token as FTS5's unicode61 tokenizer sees one (vault-mcp's store._FTS_TOKEN_RE): ASCII letters, digits and the
# underscore, and anything beyond ASCII. Every ASCII separator, the double quote among them, falls outside.
_TOKEN_RE = re.compile(r"[0-9A-Za-z_-￿]+")


def fts_query(text: str) -> str:
    """The FTS5 MATCH expression for what was typed: vault-mcp's, with the last word as a prefix.

    Every word is quoted, so nothing typed reaches FTS5's query parser (``nas-tunnel`` is a syntax error there, ``a:b``
    a column filter), and a word joined by punctuation stays one phrase. The words are ANDed, so a passage holds them
    all or isn't a words hit. The last is still being typed, so it matches as a prefix unless a space follows it.
    "" when nothing typed is a word.
    """
    terms: list[str] = []
    for word in text.split():
        parts = _TOKEN_RE.findall(word)
        if parts:
            terms.append('"' + " ".join(parts) + '"')
            if len(terms) >= MAX_TERMS:
                break
    if terms and not text[-1:].isspace():
        terms[-1] += "*"
    return " AND ".join(terms)


_WIKILINK_RE = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_MDLINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Emphasis, code ticks, table pipes, and a line's heading, quote or list marker. An underscore inside a word stays.
_MARKS_RE = re.compile(r"\*+|~~|`+|\||(?<!\w)_+|_+(?!\w)|^[ \t]{0,3}(?:#{1,6}|>+|[-+]|\d+[.)])[ \t]+", re.MULTILINE)


def plain(text: str) -> str:
    """A passage or heading as the page shows it: Markdown's syntax out, links as their words, whitespace collapsed."""
    text = _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    text = _MDLINK_RE.sub(r"\1", text)
    text = _MARKS_RE.sub(" ", _HTML_TAG_RE.sub(" ", text))
    return " ".join(text.split())


def snippet(text: str, query: str) -> str:
    """About SNIPPET_LEN of a passage: around the first place a word of ``query`` starts a word in it, else its start.

    At a word's start, as the words leg matched it: "on" is no reason to quote the middle of "rationale".
    """
    line = plain(text)
    words = [re.escape(word) for word in query.split() if len(word) > 1]
    hit = re.search(r"(?<!\w)(?:" + "|".join(words) + ")", line, re.IGNORECASE) if words else None
    at = hit.start() if hit else 0
    start = 0 if at < 40 else line.rfind(" ", 0, at - 30) + 1
    end = start + SNIPPET_LEN
    if end < len(line):
        space = line.rfind(" ", start, end)
        end = space if space > start + SNIPPET_LEN // 2 else end
    return ("…" if start else "") + line[start:end].strip() + ("…" if end < len(line) else "")


# MARK: - Pages


def place(path: str, notes: Any, artifacts: Any) -> dict[str, Any] | None:
    """Where the sidebar lists a passage's page: its row's fields, or None when neither vault lists it.

    vault-mcp names a note by its path in the Notes vault, and an artifact by ``Artifacts/`` and its path in Artifacts,
    as Onyx's two trees (``vault.VaultIndex``) list them. So a path is looked up in a tree rather than joined onto a
    folder, and only a page a tree lists (a file that exists, inside its vault) comes back. A Notes folder named
    Artifacts shadows the mount in vault-mcp too, so Notes is asked first.
    """
    if notes is not None:
        item = notes.at(path)
        if item is not None and item.kind == "note" and not item.missing:
            return {"vault": "notes", "path": str(item.path), "title": item.label, "folder": item.folder}
    mount, _, rest = path.partition("/")
    if artifacts is not None and mount == ARTIFACTS_MOUNT and rest:
        item = artifacts.at(rest)
        if item is not None and not item.missing:
            return {
                "vault": "html",
                "path": str(item.path),
                "title": item.label,
                "folder": item.entry_rel.rpartition("/")[0],
            }
    return None


def place_in(tree: Any, vault: str) -> Callable[[str], dict[str, Any] | None]:
    """``place`` for another notes vault, searched through its own index, which names a note by its path in that
    vault; ``vault`` is the key its rows carry (``app._Vault.key``)."""

    def place_one(path: str) -> dict[str, Any] | None:
        item = tree.at(path)
        if item is None or item.kind != "note" or item.missing:
            return None
        return {"vault": vault, "path": str(item.path), "title": item.label, "folder": item.folder}

    return place_one


def locate(path: str, kind: str, tree: Any) -> str | None:
    """vault-mcp's name for a page Onyx is showing, or None when the tree doesn't list it: ``place`` the other way.

    ``path`` is the page as Onyx names it — a row's ``path``, which is what the reader carries as its ``src``: the
    lexical path inside the vault, links unresolved. A page reached by the file it really is instead (the reading
    history keys a document by its realpath) comes back through the tree's ``by_real``.
    """
    if tree is None or not path:
        return None
    item = None
    root = str(tree.root)
    lexical = os.path.normpath(path)
    if os.path.isabs(lexical) and lexical.startswith(root + os.sep):
        item = tree.at(lexical[len(root) + 1 :].replace(os.sep, "/"))
    if item is None:
        item = tree.by_real(path)
    if item is None or item.missing:
        return None
    return f"{ARTIFACTS_MOUNT}/{item.rel}" if kind == "html" else item.rel


def sections(heading_path: str, title: Any) -> tuple[str, str]:
    """A passage's own heading, and the trail of headings above it without the page's title repeated at its head."""
    trail = [plain(part) for part in heading_path.split(" > ")]
    trail = [part for part in trail if part]
    rest = trail[1:] if trail and trail[0].casefold() == str(title or "").casefold() else trail
    return (trail[-1] if trail else ""), " › ".join(rest)


# A copy's tail: "AEG Sync 04.28.26 2.md" beside "AEG Sync 04.28.26.md", and what Finder and Obsidian add.
_COPY_TAIL_RE = re.compile(r"[ _-](?:copy|copy \d{1,2}|\(\d{1,2}\)|\d{1,2})$")


def document_name(path: str) -> str:
    """What an index path calls its document: the filename, without its format and without a copy's tail.

    ``Jev.md`` and ``Jev.html`` are one document under two formats, and ``AEG Sync 04.28.26 2.md`` is a copy of one,
    so all three answer the same thing. What that is worth depends on how many pages answer it too (``_Passages``).
    """
    base = path.rpartition("/")[2]
    base = base[: base.rfind(".")] if "." in base else base
    return _COPY_TAIL_RE.sub("", base.casefold()).strip()


def centroid(vecs: list[array.array], cap: int = CENTROID_SAMPLE) -> array.array:
    """The corpus baseline: the average direction of ``vecs``, from a stride sample of at most ``cap`` of them.

    A centroid's direction settles long before the whole corpus is in — 2000 of the author's 11k passages give a
    direction 0.9999 of the way to the full one — and the sample is a stride rather than a draw so that an index
    always yields the same baseline, and so the same scores.
    """
    if not vecs:
        return array.array("f")
    picked = vecs[:: max(1, len(vecs) // cap)]
    total = list(map(sum, zip(*picked)))
    norm = math.sqrt(_dot(total, total))
    return array.array("f", (value / norm for value in total)) if norm else array.array("f")


def direction(vecs: Sequence[Sequence[float]]) -> list[float] | None:
    """The unit mean of ``vecs``: a page's direction, from its passages'. None when they cancel out."""
    total = [sum(column) for column in zip(*vecs)]
    norm = math.sqrt(_dot(total, total)) if total else 0.0
    return [value / norm for value in total] if norm else None


def against_baseline(raw: float, source_mu: float, page_mu: float) -> float:
    """``raw`` with the corpus baseline taken out: the cosine between the two vectors once both are centred on it.

    The identity, for unit ``q``, ``v`` and baseline ``m``: (q−m)·(v−m) = q·v − q·m − v·m + 1, and |q−m| = √(2−2q·m).
    So one stored number per passage (its own ``q·m``, the ``hub``) is enough to centre the whole index at the moment
    a page is scored, without a second copy of 11k vectors in memory.
    """
    spread = math.sqrt(max(0.0, 2 - 2 * source_mu)) * math.sqrt(max(0.0, 2 - 2 * page_mu))
    return (raw - source_mu - page_mu + 1) / spread if spread > 1e-9 else 0.0


# MARK: - The index


class Unavailable(Exception):
    """A leg can't run. The message says why, in words for the palette's footer."""


class _Superseded(Exception):
    """A newer query arrived while this one's meaning leg was scanning."""


@dataclass
class _Passages:
    stamp: tuple
    model: str
    dim: int = 0
    pos: dict[int, int] = field(default_factory=dict)  # chunk id -> position, the words leg's way in
    pages: dict[str, list[int]] = field(default_factory=dict)  # file path -> its passages, the related pane's way in
    folded: dict[str, str] = field(default_factory=dict)  # casefolded file path -> the path as the index spells it
    paths: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    vecs: list[array.array] = field(default_factory=list)
    mu: array.array = field(default_factory=lambda: array.array("f"))  # the corpus baseline
    hub: list[float] = field(default_factory=list)  # each passage's own cosine to it
    names: dict[str, int] = field(default_factory=dict)  # document name -> how many pages answer to it


def fuse(by_words: list[int], by_meaning: list[int], cosine: dict[int, float]) -> list[tuple[int, str]]:
    """vault-mcp's Reciprocal Rank Fusion, best first, each passage with how it was found: words, meaning or both.

    Meaning takes part only above MEANING_FLOOR: below it, a passage's rank among the meaning candidates is noise.
    On an exact tie the words hit goes first, as in vault-mcp: the words leg can abstain, and the meaning leg cannot.
    """
    by_meaning = [p for p in by_meaning if cosine.get(p, 0.0) >= MEANING_FLOOR]
    fused: dict[int, float] = {}
    for positions in (by_meaning, by_words):
        for rank, p in enumerate(positions, start=1):
            fused[p] = fused.get(p, 0.0) + 1.0 / (RRF_K + rank)
    words, meaning = set(by_words), set(by_meaning)
    order = sorted(fused, key=lambda p: (-fused[p], p not in words, p))
    return [(p, "both" if p in words and p in meaning else "words" if p in words else "meaning") for p in order]


class PassageIndex:
    """vault-mcp's index, read-only: loaded on first use, and again whenever it has been rebuilt.

    ``embed(model, text)`` turns a query into a vector; by default it asks Ollama. Tests hand in their own.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        ollama: str = DEFAULT_OLLAMA,
        embed: Callable[[str, str], Sequence[float]] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.ollama = ollama.rstrip("/")
        self._embed = embed or self._ollama_embed
        self._lock = threading.Lock()
        self._data: _Passages | None = None
        self._checked = 0.0
        self._queries = itertools.count(1)
        self._latest = 0
        # The related pane counts its own scans: it and the palette scan the same passages, and neither is a newer
        # version of the other, so a page opening behind the palette must not cancel the query being typed into it.
        self._relates = itertools.count(1)
        self._latest_related = 0

    def _connect(self) -> sqlite3.Connection:
        uri = "file:" + urllib.parse.quote(str(self.db_path)) + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=False)

    @staticmethod
    def _stamp(conn: sqlite3.Connection) -> tuple:
        # vault-mcp writes last_index_time when a reindex finishes, and chunk ids only grow, so a new passage moves the
        # max before then. (A removed one waits for the reindex to finish; the words leg drops ids it doesn't know.)
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_index_time'").fetchone()
        return (row[0] if row else "", conn.execute("SELECT max(id) FROM chunks").fetchone()[0])

    @staticmethod
    def _load(conn: sqlite3.Connection, stamp: tuple) -> _Passages:
        row = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        data = _Passages(stamp=stamp, model=(row[0] if row else "") or DEFAULT_MODEL)
        rows = conn.execute("SELECT id, file_path, heading_path, text, embedding FROM chunks ORDER BY id")
        for chunk_id, path, heading, text, blob in rows:
            if not isinstance(blob, bytes) or not blob or len(blob) % 4:
                continue
            vec = array.array("f")
            vec.frombytes(blob)  # float32, as vault-mcp stores them: already normalised
            data.dim = data.dim or len(vec)
            if len(vec) != data.dim:
                continue
            data.pos[chunk_id] = len(data.paths)
            data.pages.setdefault(path, []).append(len(data.paths))
            data.folded.setdefault(path.casefold(), path)
            data.paths.append(path)
            data.headings.append(heading or "")
            data.texts.append(text or "")
            data.vecs.append(vec)
        # These embeddings share a strong common direction — two unrelated passages of the author's vault still score
        # 0.635 against each other — and the passages nearest that direction sit near everything, so a long, diffuse
        # page (a playbook, a meeting transcript) turns up beside subjects it has nothing to do with. Holding each
        # passage's cosine to the baseline is what lets the related pane take it back out.
        data.mu = centroid(data.vecs)
        data.hub = [_dot(data.mu, vec) for vec in data.vecs] if data.mu else [0.0] * len(data.vecs)
        for page in data.pages:
            name = document_name(page)
            data.names[name] = data.names.get(name, 0) + 1
        return data

    def _passages(self) -> _Passages:
        with self._lock:
            now = time.monotonic()
            if self._data is not None and now - self._checked < RELOAD_EVERY:
                return self._data
            if not self.db_path.is_file():
                self._data = None
                raise Unavailable(f"no vault index at {_short(self.db_path)}")
            try:
                with closing(self._connect()) as conn:
                    stamp = self._stamp(conn)
                    if self._data is None or self._data.stamp != stamp:
                        started = time.perf_counter()
                        self._data = self._load(conn, stamp)
                        logger.info(
                            "Read %d passages from %s in %.0f ms",
                            len(self._data.paths), self.db_path, (time.perf_counter() - started) * 1000,
                        )
            except sqlite3.Error as exc:
                self._data = None
                raise Unavailable(f"the vault index can't be read ({exc})") from exc
            self._checked = now
            return self._data

    # MARK: the two legs

    def _words(self, data: _Passages, query: str) -> list[tuple[int, float]]:
        """The words leg: each candidate passage's position with its BM25 rank (lower is better), best first."""
        match = fts_query(query)
        if not match:
            return []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT rowid, bm25(chunks_fts, 1.0, 0.5) AS rank FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match, CANDIDATES),
                ).fetchall()
        except sqlite3.Error as exc:
            raise Unavailable(f"the vault index has no words to search ({exc})") from exc
        return [(data.pos[row[0]], row[1]) for row in rows if row[0] in data.pos]

    def _ollama_embed(self, model: str, text: str) -> Sequence[float]:
        body = json.dumps({"model": model, "input": [QUERY_PREFIX + text], "keep_alive": KEEP_ALIVE}).encode()
        request = urllib.request.Request(
            self.ollama + "/api/embed", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _OPENER.open(request, timeout=EMBED_TIMEOUT) as response:
                return json.load(response)["embeddings"][0]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise Unavailable(f"Ollama doesn't have {model} (ollama pull {model})") from exc
            raise Unavailable(f"Ollama refused the query (HTTP {exc.code})") from exc
        except (urllib.error.URLError, OSError) as exc:
            slow = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            raise Unavailable("Ollama took too long to answer" if slow else "Ollama isn't running") from exc
        except (ValueError, LookupError, TypeError) as exc:
            raise Unavailable("Ollama sent back no embedding") from exc

    def _query_vector(self, data: _Passages, text: str) -> array.array:
        raw = self._embed(data.model, text)
        norm = math.sqrt(_dot(raw, raw)) if len(raw) == data.dim else 0.0
        if not norm:
            raise Unavailable(f"{data.model} gave a vector this index can't use")
        return array.array("f", (x / norm for x in raw))

    def _meaning(self, data: _Passages, qvec: array.array, query_id: int) -> tuple[list[int], dict[int, float]]:
        scores: list[float] = []
        for start in range(0, len(data.vecs), SCAN_BLOCK):
            if self._latest != query_id:
                raise _Superseded
            scores.extend(_dot(qvec, vec) for vec in data.vecs[start : start + SCAN_BLOCK])
        top = heapq.nlargest(CANDIDATES, range(len(scores)), key=scores.__getitem__)
        return top, {p: scores[p] for p in top}

    # MARK: what the palette asks

    def search(
        self, query: str, *, place: Callable[[str], dict[str, Any] | None], limit: int = 12
    ) -> dict[str, Any]:
        """Pages whose passages match ``query``, best first, one row each.

        ``place`` turns vault-mcp's path into the row's fields (``search.place`` over Onyx's trees), or None for a page
        neither vault lists. Each row adds its passage: the heading it sits under, the trail of headings above it, a
        snippet, and how it matched. ``words`` and ``meaning`` say whether each leg could run, and why not.
        ``superseded`` means a newer query came in first and this one was dropped.
        """
        return search_many([(self, place, "")], query, limit=limit)

    def _begin(self) -> int:
        """A new palette query: from here on, an older one's meaning scan stops at its next block."""
        query_id = next(self._queries)
        self._latest = query_id
        return query_id

    def related(self, path: str, *, place: Callable[[str], dict[str, Any] | None], limit: int = 20) -> dict[str, Any]:
        """The pages nearest ``path`` in meaning, best first: the neighbours of the page being read.

        ``path`` is vault-mcp's name for the page (``locate``). Its passages average into one direction, and every
        other page is scored against it at its best passage — vault-mcp's ``related_notes``, over the index Onyx
        already has open. Nothing is embedded, since the page's own vectors are in the index, so this asks nothing of
        Ollama and works while it is off.

        Scores are taken against the corpus baseline (``against_baseline``), so 0 is "no closer than any two pages in
        this vault" and everything returned is above ``RELATED_FLOOR`` — a page with nothing near it comes back empty
        rather than padded out with its own tail. ``dupe`` marks a neighbour near enough to be the same content twice.
        ``near`` says how related the first ``RELATED_MAP`` neighbours are to one another, for the map (``_near``).
        ``reason`` says why there are no rows: a page too short to have been indexed, or one not indexed yet, has no
        direction to search from.
        """
        scan_id = next(self._relates)
        self._latest_related = scan_id
        result: dict[str, Any] = {"note": path, "items": [], "floor": RELATED_FLOOR, "near": [], "reason": ""}
        try:
            data = self._passages()
        except Unavailable as exc:
            result["reason"] = str(exc)
            return result
        own = data.pages.get(path) or data.pages.get(data.folded.get(path.casefold(), ""), [])
        if not own or not data.dim:
            result["reason"] = "this page isn't in the vault index yet"
            return result
        mean = [0.0] * data.dim
        for p in own:
            for i, value in enumerate(data.vecs[p]):
                mean[i] += value
        norm = math.sqrt(_dot(mean, mean))
        if not norm:
            result["reason"] = "this page's passages point nowhere"
            return result
        centre = array.array("f", (value / norm for value in mean))
        source_mu = _dot(centre, data.mu) if data.mu else 0.0
        own_name = document_name(path)
        # A name is evidence of one document only where the vault uses it for one document (DUPE_NAME_SHARED_BY).
        named = own_name if data.names.get(own_name, 0) <= DUPE_NAME_SHARED_BY else ""
        # Each page at its best passage, as the palette lists one: the whole scan, in blocks, so a page opened while
        # an older one is still being scored drops the older scan rather than queue behind it.
        skip, best = set(own), {}
        for start in range(0, len(data.vecs), SCAN_BLOCK):
            if self._latest_related != scan_id:
                result["superseded"] = True
                return result
            for p in range(start, min(start + SCAN_BLOCK, len(data.vecs))):
                if p in skip:
                    continue
                score = against_baseline(_dot(centre, data.vecs[p]), source_mu, data.hub[p])
                page = data.paths[p]
                if score > best.get(page, (-2.0, 0))[0]:
                    best[page] = (score, p)
        listed: list[str] = []
        for page in sorted(best, key=lambda page: (-best[page][0], page)):
            if best[page][0] < RELATED_FLOOR:
                break  # the tail every page has, and no reason to open anything
            row = place(page)
            if row is None:
                continue  # indexed, but neither tree lists it: a note outside the vaults Onyx is showing
            score, p = best[page]
            heading, section = sections(data.headings[p], row.get("title", ""))
            result["items"].append(
                {
                    **row,
                    "score": round(score, 3),
                    "dupe": score >= DUPE_AT or (score >= DUPE_NAMED and bool(named) and document_name(page) == named),
                    "heading": heading,
                    "section": section,
                    "snippet": snippet(data.texts[p], ""),
                }
            )
            listed.append(page)
            if len(result["items"]) >= limit:
                break
        if len(listed) > 1:
            result["near"] = self._near(data, listed[:RELATED_MAP])
        return result

    @staticmethod
    def _near(data: _Passages, pages: list[str]) -> list[list[float]]:
        """How related each of ``pages`` is to each of the others: the map's other half, beside each one's ``score``.

        One page scores another as ``related`` scores a neighbour — from the first page's direction, at the other's
        nearest passage, with the baseline out — and the two ways round are averaged, since the map draws one
        distance per pair. It is what lets the map put the neighbours that are alike near each other (radial stress,
        vault_ui.py). Measured over 80 of the author's pages, a layout from these draws pairs of neighbours at
        distances that rank-agree 0.67 with them; Smart Connections' four k-means corners manage 0.36, and angles
        chosen at random 0.14.
        """
        heads = [direction([data.vecs[p] for p in data.pages[page]]) for page in pages]

        def one_way(i: int, j: int) -> float:
            head = heads[i]
            if head is None:
                return 0.0
            head_mu = _dot(head, data.mu) if data.mu else 0.0
            return max(against_baseline(_dot(head, data.vecs[p]), head_mu, data.hub[p]) for p in data.pages[pages[j]])

        near = [[1.0] * len(pages) for _ in pages]
        for i in range(len(pages)):
            for j in range(i + 1, len(pages)):
                near[i][j] = near[j][i] = round((one_way(i, j) + one_way(j, i)) / 2, 3)
        return near

    def knows(self, path: str) -> bool:
        """Whether the index holds passages under this name.

        One file can have more than one name here: an Artifacts page is usually a link, and when it points into the
        Notes vault the same bytes are both ``Artifacts/…`` and a note. vault-mcp indexes one of them — it leaves out
        a vault HTML file that is the rendering of a same-name ``.md`` — so which name to ask about is a question only
        the index can answer, and guessing it from the vault the reader happens to be in gets it wrong.
        """
        try:
            data = self._passages()
        except Unavailable:
            return False
        return path in data.pages or path.casefold() in data.folded

    def status(self, *, warm: bool = False) -> dict[str, Any]:
        """Which legs a search would run, and why one wouldn't.

        ``warm`` (the palette opening) reads the index in and wakes the embedding model, so the first query typed
        doesn't wait on either.
        """
        words: dict[str, Any] = {"ok": True, "reason": ""}
        meaning: dict[str, Any] = {"ok": True, "reason": ""}
        try:
            data = self._passages()
        except Unavailable as exc:
            words.update(ok=False, reason=str(exc))
            meaning.update(ok=False, reason=str(exc))
            return {"words": words, "meaning": meaning, "passages": 0}
        if warm:
            try:
                self._query_vector(data, "")
            except Unavailable as exc:
                meaning.update(ok=False, reason=str(exc))
        return {"words": words, "meaning": meaning, "passages": len(data.paths)}


def search_many(
    sources: Sequence[tuple[PassageIndex, Callable[[str], dict[str, Any] | None], str]],
    query: str,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    """``PassageIndex.search`` over one index or several at once: one list, best first, each page once.

    ``sources`` are ``(index, place, label)``, the first being the primary vault's: its legs' state is the one reported,
    as with one index. Another that can't be read is named in ``missing`` (``label: why``) and the rest still answer.

    The legs are pooled before they are fused, so a page ranks against every vault's rather than taking turns with
    them. Meaning pools exactly, since every index is embedded by the same model and a cosine means the same in each.
    Words pool by BM25, whose term weights are each index's own, so between vaults it is close rather than exact. With
    one index both pools are that index's own lists, in their own order, and the result is what one index gives.
    """
    ids = [index._begin() for index, _, _ in sources]
    words: dict[str, Any] = {"ok": True, "reason": ""}
    meaning: dict[str, Any] = {"ok": True, "reason": ""}
    result: dict[str, Any] = {"items": [], "words": words, "meaning": meaning}
    text = query.strip()
    loaded: dict[int, _Passages] = {}
    for i, (index, _, label) in enumerate(sources):
        try:
            loaded[i] = index._passages()
        except Unavailable as exc:
            if i == 0:
                words.update(ok=False, reason=str(exc))
                meaning.update(ok=False, reason=str(exc))
                return result
            result.setdefault("missing", []).append(f"{label}: {exc}")
    pooled_words: list[tuple[float, tuple[int, int]]] = []
    if len(text) >= MIN_WORDS:
        for i, data in loaded.items():
            try:
                pooled_words += [(rank, (i, p)) for p, rank in sources[i][0]._words(data, query)]
            except Unavailable as exc:
                if i == 0:
                    words.update(ok=False, reason=str(exc))
    # Stable, so one index keeps its own order (and a tie between vaults, the vaults' order).
    by_words = [key for _, key in sorted(pooled_words, key=lambda hit: hit[0])[:CANDIDATES]]
    pooled_meaning: list[tuple[float, tuple[int, int]]] = []
    if len(text) >= MIN_MEANING:
        vectors: dict[tuple[str, int], array.array] = {}  # one embedding per model, however many indexes use it
        for i, data in loaded.items():
            if not data.vecs:
                continue
            index = sources[i][0]
            try:
                qvec = vectors.get((data.model, data.dim))
                if qvec is None:
                    qvec = vectors[(data.model, data.dim)] = index._query_vector(data, text)
                top, scores = index._meaning(data, qvec, ids[i])
            except Unavailable as exc:
                if i == 0:
                    meaning.update(ok=False, reason=str(exc))
                elif meaning["ok"]:
                    result.setdefault("missing", []).append(f"{sources[i][2]}: {exc}")
                continue
            except _Superseded:
                result["superseded"] = True
                return result
            pooled_meaning += [(scores[p], (i, p)) for p in top]
    pooled_meaning.sort(key=lambda hit: -hit[0])
    by_meaning = [key for _, key in pooled_meaning[:CANDIDATES]]
    cosine = {key: score for score, key in pooled_meaning[:CANDIDATES]}
    seen: set[tuple[int, str]] = set()
    for (i, p), how in fuse(by_words, by_meaning, cosine):
        data = loaded[i]
        path = data.paths[p]
        if (i, path) in seen:
            continue  # a page's later passages: it already has its row, at its best one
        seen.add((i, path))
        row = sources[i][1](path)
        if row is None:
            continue
        heading, section = sections(data.headings[p], row.get("title", ""))
        result["items"].append(
            {
                **row,
                "heading": heading,
                "section": section,
                # Found by meaning alone, it holds no word typed worth quoting around: from its start.
                "snippet": snippet(data.texts[p], "" if how == "meaning" else text),
                "match": how,
            }
        )
        if len(result["items"]) >= limit:
            break
    return result
