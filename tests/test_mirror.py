"""The phone mirror's Mac side (docs/plans/phone-mirror.md): its gates, its wire format, its publisher.

The tests that seal or open a blob need the opt-in ``mirror`` extra (``cryptography``, ``segno``) and
skip without it. The rest (inert by default, credentials off argv, the signer, the repo scan) need
nothing and always run, because those are the gates that must hold on a machine that never installs it.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import io
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from onyx import storage as app_storage
from onyx.app import create_app
from onyx.config import AppConfig
from onyx.mirror import cli, service
from onyx.mirror import config as mirror_config
from onyx.mirror import secrets as keychain
from onyx.mirror.crypto import Keys, b64url_decode, deflate_raw, generate_master, inflate_raw
from onyx.mirror.errors import MirrorDependencyError, MirrorError, SecretsError, StoreError, describe_error
from onyx.mirror.publish import PublishReport, last_published, locked, publish, sealed_index_path, state_path
from onyx.mirror.secrets import MemorySecrets
from onyx.mirror.store import LocalDirStore, R2Store, canonical_request, sign_v4

try:
    import cryptography  # noqa: F401
    import segno  # noqa: F401

    HAVE_EXTRA = True
except ImportError:
    HAVE_EXTRA = False

needs_extra = unittest.skipUnless(HAVE_EXTRA, "needs the mirror extra: pip install onyx[mirror]")

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "mirror_vectors.json").read_text(encoding="utf-8"))
FIXED_NONCE = bytes([7]) * 12
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
CONFIG = mirror_config.MirrorConfig(enabled=True, interval_minutes=5, include=("Notes",), exclude=())


def hexsha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def enable(data_dir: Path, include: str = '["Notes"]') -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "mirror.toml").write_text(f"enabled = true\ninclude = {include}\n", encoding="utf-8")


# -- a stand-in for the builder, and a store that remembers what it was asked ---------------------------

@dataclass(frozen=True)
class Obj:
    """The builder's ``BuiltObject``, by its documented fields, so these tests do not need the builder."""

    name: str
    id: str
    data: bytes
    mime: str
    page: dict | None


class Built(list):
    """``build_mirror``'s result: a list of objects that also says what it could not build."""

    skipped: list[str]
    truncated: bool


class World:
    def __init__(self) -> None:
        self.items: dict[str, tuple[bytes, str, dict | None]] = {}
        self.calls: list[dict] = []
        self.skipped: list[str] = []
        self.truncated = False

    def page(self, path: str, body: str, *, title: str | None = None, mtime: float = 1_790_000_000.0,
             kind: str = "markdown", text: str | None = None) -> None:
        meta = {"path": path, "title": title or path.rsplit("/", 1)[-1], "kind": kind, "mtime": mtime,
                "text": body if text is None else text}
        self.items[f"page:{path}"] = (body.encode("utf-8"), "text/html", meta)

    def asset(self, realpath: str, data: bytes, mime: str = "image/png") -> None:
        self.items[f"asset:{realpath}"] = (data, mime, None)

    def build(self, *, roots, include, exclude, ids, markdown_css=None) -> list[Obj]:
        self.calls.append({"roots": roots, "include": include, "exclude": exclude, "markdown_css": markdown_css})
        built = Built(Obj(name, ids(name), data, mime, meta) for name, (data, mime, meta) in self.items.items())
        built.skipped, built.truncated = list(self.skipped), self.truncated
        return built


class RecordingStore(LocalDirStore):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.ops: list[tuple[str, str]] = []
        self.fail_put_number: int | None = None  # the n-th put (1-based) raises

    def put(self, id: str, data: bytes) -> None:
        self.ops.append(("put", id))
        if self.fail_put_number == sum(1 for op, _ in self.ops if op == "put"):
            raise StoreError("R2 PUT failed: HTTP 500")
        super().put(id, data)

    def delete(self, id: str) -> None:
        self.ops.append(("delete", id))
        super().delete(id)


class MirrorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()  # LocalDirStore resolves, and /var is /private/var here
        self.data = self.base / "data"
        self.data.mkdir()
        self.secrets = MemorySecrets({keychain.MASTER_KEY: VECTORS["master_key_b64url"]})

    def publish(self, world: World, store: LocalDirStore, *, now: datetime = NOW, config=CONFIG,
                css: str | None = None) -> PublishReport:
        return publish(config=config, secrets=self.secrets, store=store, roots={"Notes": self.base},
                       markdown_css=css, data_dir=self.data, build=world.build, now=now)

    def keys(self) -> Keys:
        return Keys.from_b64url(VECTORS["master_key_b64url"])

    def read_index(self, store: LocalDirStore) -> dict:
        keys = self.keys()
        blob = (store.dir / "o" / keys.ident("index")).read_bytes()
        return json.loads(inflate_raw(keys.open_blob(keys.ident("index"), blob)))


@contextmanager
def without_cryptography():
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("blocked for the test")
        return real(name, *args, **kwargs)

    with patch("builtins.__import__", refuse):
        yield


def http_error(test: unittest.TestCase, code: int, message: str = "",
               url: str = "https://example.invalid/") -> urllib.error.HTTPError:
    error = urllib.error.HTTPError(url, code, message, {}, io.BytesIO(b""))
    test.addCleanup(error.close)  # an HTTPError holds a temporary file, and a ResourceWarning if it is dropped open
    return error


def run_cli(argv: list[str], *, secrets, data_dir: Path, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin)):
        code = cli.main(argv, secrets=secrets, data_dir=data_dir)
    return code, out.getvalue(), err.getvalue()


# -- gate 1: inert without its config ------------------------------------------------------------------

class InertTests(MirrorTestCase):
    def test_mirror_is_inert_without_its_config(self) -> None:
        # No file, a file that says off, and every way a file can fail to say what to publish.
        self.assertIsNone(mirror_config.load(self.data))
        bad = [
            "enabled = false\ninclude = ['Notes']\n",
            "enabled = 'true'\ninclude = ['Notes']\n",
            "enabled = 1\ninclude = ['Notes']\n",
            "include = ['Notes']\n",
            "enabled = true\n",
            "enabled = true\ninclude = []\n",
            "enabled = true\ninclude = ['']\n",
            "enabled = true\ninclude = ['Notes', '  /  ']\n",
            "enabled = true\ninclude = 'Notes'\n",
            "enabled = true\ninclude = ['Notes']\nexclude = 'x'\n",
            "enabled = true\ninclude = [\n",
        ]
        for text in bad:
            (self.data / "mirror.toml").write_text(text, encoding="utf-8")
            self.assertIsNone(mirror_config.load(self.data), text)

        # An app started without the file does nothing: no thread, no file, no socket.
        (self.data / "mirror.toml").unlink()
        connects: list = []
        with patch("socket.socket.connect", lambda *a, **k: connects.append(a)), \
                patch.object(service, "INITIAL_DELAY", 3600):
            config = AppConfig(default_folder=self.base, allowed_roots=(self.base,), data_dir=self.data, mirror=True)
            with TestClient(create_app(config), base_url="http://127.0.0.1:8899"):
                self.assertNotIn(service.THREAD_NAME, [t.name for t in threading.enumerate()])
        self.assertEqual(connects, [])
        self.assertFalse((self.data / "mirror").exists())
        self.assertFalse((self.data / "mirror.toml").exists())

        # The publish command refuses, and says why, rather than publishing nothing quietly.
        code, out, err = run_cli(["publish"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertEqual(code, 2)
        self.assertIn("off", err)
        self.assertFalse((self.data / "mirror").exists())

    def test_the_publisher_starts_only_for_the_real_entry_point_and_an_enabled_file(self) -> None:
        def threads() -> list[str]:
            return [t.name for t in threading.enumerate() if t.name == service.THREAD_NAME]

        enable(self.data)
        with patch.object(service, "INITIAL_DELAY", 3600):
            # The default AppConfig never reads the owner's mirror.toml, so no test's app does.
            plain = AppConfig(default_folder=self.base, allowed_roots=(self.base,), data_dir=self.data)
            with TestClient(create_app(plain), base_url="http://127.0.0.1:8899"):
                self.assertEqual(threads(), [])
            real = AppConfig(default_folder=self.base, allowed_roots=(self.base,), data_dir=self.data, mirror=True)
            with TestClient(create_app(real), base_url="http://127.0.0.1:8899"):
                self.assertEqual(threads(), [service.THREAD_NAME])
            self.assertEqual(threads(), [])  # stopped with the app

    def test_interval_is_clamped_and_include_is_normalised(self) -> None:
        for text, minutes in (("0", 1), ("-5", 1), ("99999", 1440), ("7", 7), ("'5'", 5), ("true", 5), ("2.5", 5)):
            (self.data / "mirror.toml").write_text(
                f"enabled = true\ninclude = ['Notes/Areas/']\ninterval_minutes = {text}\n", encoding="utf-8")
            config = mirror_config.load(self.data)
            self.assertEqual(config.interval_minutes, minutes, text)
            self.assertEqual(config.include, ("Notes/Areas",))

    def test_stop_turns_the_switch_off_and_keeps_everything_else(self) -> None:
        path = self.data / "mirror.toml"
        path.write_text('enabled = true  # on\ninterval_minutes = 9\ninclude = ["Notes/A", "Artifacts"]\n',
                        encoding="utf-8")
        self.assertTrue(mirror_config.disable(self.data))
        self.assertIsNone(mirror_config.load(self.data))
        text = path.read_text(encoding="utf-8")
        self.assertIn('include = ["Notes/A", "Artifacts"]', text)
        self.assertIn("interval_minutes = 9", text)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        # A file the one-line edit can't reach is rebuilt from what it parsed to, and still ends up off.
        path.write_text('"enabled" = true\ninclude = ["Notes/A"]\n', encoding="utf-8")
        self.assertTrue(mirror_config.disable(self.data))
        self.assertIsNone(mirror_config.load(self.data))
        self.assertIs(mirror_config.tomllib.loads(path.read_text(encoding="utf-8"))["enabled"], False)
        self.assertIn('"Notes/A"', path.read_text(encoding="utf-8"))

        path.unlink()
        self.assertFalse(mirror_config.disable(self.data))

    def test_importing_onyx_never_imports_cryptography(self) -> None:
        code = ("import sys, onyx.app, onyx.__main__, onyx.mirror.cli, onyx.mirror.service, onyx.mirror.publish\n"
                "sys.exit(1 if 'cryptography' in sys.modules else 0)")
        done = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_without_the_extra_the_error_says_how_to_get_it(self) -> None:
        with without_cryptography():
            with self.assertRaises(MirrorDependencyError) as caught:
                Keys.derive(bytes(32))
        self.assertIn("pip install onyx[mirror]", str(caught.exception))


# -- wire format v1 ------------------------------------------------------------------------------------

@needs_extra
class CryptoTests(MirrorTestCase):
    def test_mirror_crypto_matches_the_shared_vectors(self) -> None:
        keys = self.keys()
        self.assertEqual(keys.enc.hex(), VECTORS["k_enc_hex"])
        self.assertEqual(keys.id.hex(), VECTORS["k_id_hex"])
        for name, expected in VECTORS["ids"].items():
            self.assertEqual(keys.ident(name), expected, name)
            self.assertRegex(expected, r"^[0-9a-f]{64}$")

        page = VECTORS["page"]
        blob = keys.seal(page["id"], page["plaintext_utf8"].encode("utf-8"), nonce=FIXED_NONCE)
        self.assertEqual(base64.b64encode(blob).decode(), page["blob_b64"])
        self.assertEqual(hexsha(blob), VECTORS["index"]["json"]["pages"][0]["sha"])
        self.assertEqual(len(blob), VECTORS["index"]["json"]["pages"][0]["size"])
        self.assertEqual(keys.open_blob(page["id"], blob).decode("utf-8"), page["plaintext_utf8"])

        index = VECTORS["index"]
        packed = deflate_raw(json.dumps(index["json"], ensure_ascii=False).encode("utf-8"))
        sealed = keys.seal(index["id"], packed, nonce=FIXED_NONCE)
        self.assertEqual(base64.b64encode(sealed).decode(), index["blob_b64"])
        self.assertEqual(json.loads(inflate_raw(keys.open_blob(index["id"], sealed))), index["json"])

        # The id is the associated data: a store that swaps two objects makes both fail to open.
        with self.assertRaises(MirrorError):
            keys.open_blob(index["id"], blob)
        with self.assertRaises(MirrorError):
            keys.open_blob(page["id"], base64.b64decode(index["blob_b64"]))
        tampered = bytearray(blob)
        tampered[20] ^= 1
        with self.assertRaises(MirrorError):
            keys.open_blob(page["id"], bytes(tampered))
        with self.assertRaises(MirrorError):
            keys.open_blob(page["id"], blob[:20])
        with self.assertRaises(MirrorError):
            Keys.derive(bytes(32)).open_blob(page["id"], blob)

    def test_a_fresh_seal_uses_a_fresh_nonce(self) -> None:
        keys = self.keys()
        one, two = keys.seal("a" * 64, b"same"), keys.seal("a" * 64, b"same")
        self.assertNotEqual(one, two)
        self.assertEqual(keys.open_blob("a" * 64, one), keys.open_blob("a" * 64, two))

    def test_generated_master_keys_are_32_bytes_of_unpadded_base64url(self) -> None:
        master = generate_master()
        self.assertNotIn("=", master)
        self.assertEqual(len(b64url_decode(master)), 32)
        self.assertNotEqual(master, generate_master())


class PairingTests(MirrorTestCase):
    def test_mirror_pairing_string_matches_the_shared_vector(self) -> None:
        pairing = VECTORS["pairing"]
        secrets = MemorySecrets({keychain.MASTER_KEY: VECTORS["master_key_b64url"],
                                 keychain.READ_TOKEN: pairing["t"], keychain.WORKER_URL: pairing["u"] + "/"})
        text = cli.pairing_string(secrets)
        self.assertEqual(text, pairing["string"])
        body = json.loads(b64url_decode(text[len("onyxmirror1:"):]))
        self.assertEqual(body, {"u": pairing["u"], "t": pairing["t"], "k": VECTORS["master_key_b64url"]})


# -- the publisher -------------------------------------------------------------------------------------

@needs_extra
class PublishTests(MirrorTestCase):
    def test_mirror_sends_no_plaintext(self) -> None:
        canary = "Quokka-Ünïcode-7f3a"
        world = World()
        world.page(f"Notes/{canary}/{canary}.md", f"<html><body><p>{canary} body</p></body></html>",
                   title=f"{canary} title", text=f"{canary} text")
        world.asset(f"/Users/x/{canary}/pic.png", f"PNG {canary}".encode("utf-8"))
        store = RecordingStore(self.base / "out")
        self.publish(world, store)

        forms = [canary, canary.lower(), canary.upper(), urllib.parse.quote(canary), canary.encode("utf-8").hex()]
        needles = [f.encode("utf-8") for f in forms] + [canary.encode("utf-16-le"), canary.encode("utf-16-be")]
        written = [p for p in store.dir.rglob("*") if p.is_file()]
        self.assertEqual(len(written), 3)  # the page, the asset and the index; no leftovers
        for path in [*written, *self.data.rglob("*")]:
            relative = str(path.relative_to(self.base)).encode("utf-8")
            body = path.read_bytes() if path.is_file() else b""
            for needle in needles:
                self.assertNotIn(needle, relative, "a name reveals the canary")
                self.assertNotIn(needle, body, f"{path.name} holds the canary")
        for path in written:
            self.assertRegex(path.name, r"^[0-9a-f]{64}$")

        # The same test must be able to see it where it does belong: inside the sealed index.
        index = self.read_index(store)
        self.assertIn(canary, json.dumps(index, ensure_ascii=False))
        self.assertEqual(index["pages"][0]["text"], f"{canary} text")

    def test_objects_open_back_to_what_was_built_and_the_index_follows_the_wire_format(self) -> None:
        world = World()
        world.page("Notes/B/Two.md", "<p>two</p>", title="Two", kind="markdown", text="two")
        world.page("Artifacts/One/index.html", "<p>one</p>", title="One", kind="html", mtime=12.5, text="x" * 30_000)
        world.asset("/Users/x/pic.png", b"\x89PNG-bytes")
        store = RecordingStore(self.base / "out")
        report = self.publish(world, store)
        self.assertEqual((report.pages, report.assets, report.uploaded, report.unchanged, report.deleted),
                         (2, 1, 3, 0, 0))
        self.assertTrue(report.index_uploaded)

        keys = self.keys()
        index = self.read_index(store)
        self.assertEqual(list(index), ["v", "published_at", "pages", "assets"])
        self.assertEqual((index["v"], index["published_at"]), (1, "2026-09-30T12:00:00Z"))
        self.assertEqual([p["path"] for p in index["pages"]], ["Artifacts/One/index.html", "Notes/B/Two.md"])
        first = index["pages"][0]
        self.assertEqual(list(first), ["id", "path", "title", "kind", "sha", "size", "mtime", "text"])
        self.assertEqual(len(first["text"]), 20_000)
        self.assertEqual((first["kind"], first["mtime"]), ("html", 12.5))
        self.assertEqual(first["id"], keys.ident("page:Artifacts/One/index.html"))
        self.assertEqual(index["assets"], [{"id": keys.ident("asset:/Users/x/pic.png"), "mime": "image/png",
                                            "sha": index["assets"][0]["sha"], "size": index["assets"][0]["size"]}])
        for entry in [*index["pages"], *index["assets"]]:
            blob = (store.dir / "o" / entry["id"]).read_bytes()
            self.assertEqual((hexsha(blob), len(blob)), (entry["sha"], entry["size"]))
        self.assertEqual(keys.open_blob(first["id"], (store.dir / "o" / first["id"]).read_bytes()), b"<p>one</p>")
        asset = index["assets"][0]
        self.assertEqual(keys.open_blob(asset["id"], (store.dir / "o" / asset["id"]).read_bytes()), b"\x89PNG-bytes")

        # What the builder was asked for: the config's lists, the roots, the vault CSS.
        call = world.calls[0]
        self.assertEqual((call["include"], call["exclude"]), (["Notes"], []))
        self.assertEqual(list(call["roots"]), ["Notes"])

    def test_mirror_reuploads_only_changed_objects(self) -> None:
        world = World()
        world.page("Notes/A.md", "<p>a</p>", text="a")
        world.page("Notes/B.md", "<p>b</p>", text="b")
        world.asset("/x/pic.png", b"pic")
        store = RecordingStore(self.base / "out")
        keys = self.keys()
        index_id = keys.ident("index")
        a, b = keys.ident("page:Notes/A.md"), keys.ident("page:Notes/B.md")
        blob = lambda id_: (store.dir / "o" / id_).read_bytes()  # noqa: E731

        first = self.publish(world, store)
        self.assertEqual((first.uploaded, first.unchanged, first.index_uploaded), (3, 0, True))
        puts = [i for op, i in store.ops if op == "put"]
        self.assertEqual(puts[-1], index_id, "the index goes up after every object it names")
        self.assertEqual(len(puts), 4)

        # Nothing changed: nothing sent, the index untouched even though the clock moved on.
        store.ops.clear()
        a_blob, b_blob, index_blob = blob(a), blob(b), blob(index_id)
        later = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)
        second = self.publish(world, store, now=later)
        self.assertEqual((second.uploaded, second.unchanged, second.deleted, second.index_uploaded, second.bytes),
                         (0, 3, 0, False, 0))
        self.assertEqual(store.ops, [])
        self.assertEqual(blob(index_id), index_blob)
        self.assertEqual(self.read_index(store)["published_at"], "2026-09-30T12:00:00Z")

        # One page changed: that object and the index, nothing else, and the other blobs keep their bytes.
        world.page("Notes/A.md", "<p>a, edited</p>", text="a, edited")
        third = self.publish(world, store, now=later)
        self.assertEqual((third.uploaded, third.unchanged, third.index_uploaded), (1, 2, True))
        self.assertEqual(store.ops, [("put", a), ("put", index_id)])
        self.assertNotEqual(blob(a), a_blob)
        self.assertEqual(blob(b), b_blob)
        index = self.read_index(store)
        self.assertEqual(index["published_at"], "2026-09-30T12:05:00Z")
        self.assertEqual(next(p for p in index["pages"] if p["id"] == a)["sha"], hexsha(blob(a)))
        self.assertEqual(next(p for p in index["pages"] if p["id"] == b)["sha"], hexsha(b_blob))

        # A touched file changes the index (its mtime) and nothing else.
        store.ops.clear()
        world.page("Notes/B.md", "<p>b</p>", text="b", mtime=1_790_000_999.0)
        self.assertEqual(self.publish(world, store).uploaded, 0)
        self.assertEqual(store.ops, [("put", index_id)])

        # A page removed: the new index first, then the delete.
        store.ops.clear()
        del world.items["page:Notes/B.md"]
        fourth = self.publish(world, store)
        self.assertEqual((fourth.uploaded, fourth.deleted, fourth.index_uploaded), (0, 1, True))
        self.assertEqual(store.ops, [("put", index_id), ("delete", b)])
        self.assertFalse((store.dir / "o" / b).exists())
        self.assertEqual([p["path"] for p in self.read_index(store)["pages"]], ["Notes/A.md"])

    def test_a_failed_upload_is_picked_up_again_without_resending_what_got_there(self) -> None:
        world = World()
        for name in "abc":
            world.page(f"Notes/{name}.md", f"<p>{name}</p>")
        store = RecordingStore(self.base / "out")
        store.fail_put_number = 2
        with self.assertRaises(StoreError):
            self.publish(world, store)
        self.assertEqual(len(list((store.dir / "o").iterdir())), 1)
        self.assertFalse((store.dir / "o" / self.keys().ident("index")).exists(), "no index over missing objects")

        store.fail_put_number = None
        store.ops.clear()
        report = self.publish(world, store)
        self.assertEqual((report.uploaded, report.unchanged, report.index_uploaded), (2, 1, True))
        self.assertEqual(len([op for op in store.ops if op[0] == "put"]), 3)  # two objects and the index

    def publish_three(self, world: World, store: RecordingStore) -> dict:
        world.page("Notes/A.md", "<p>a</p>", text="a")
        world.page("Notes/B.md", "<p>b</p>", text="b words")
        world.asset("/x/pic.png", b"pic")
        self.publish(world, store)
        return self.read_index(store)

    def test_mirror_keeps_pages_that_failed_to_build(self) -> None:
        world, store, keys = World(), RecordingStore(self.base / "out"), self.keys()
        first = self.publish_three(world, store)
        b = keys.ident("page:Notes/B.md")
        b_blob = (store.dir / "o" / b).read_bytes()

        # B could not be read this time, and so its asset was not collected either.
        del world.items["page:Notes/B.md"], world.items["asset:/x/pic.png"]
        world.skipped = ["Notes/B.md"]
        store.ops.clear()
        report = self.publish(world, store)
        self.assertEqual(store.ops, [], "nothing is sent and nothing deleted: the index still says what it said")
        self.assertEqual((report.pages, report.assets, report.deleted), (2, 1, 0))
        self.assertEqual(self.read_index(store), first)

        # The same, while another page changes: the new index still lists B, as it was.
        world.page("Notes/A.md", "<p>a, edited</p>", text="a, edited")
        later = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)
        store.ops.clear()
        self.publish(world, store, now=later)
        self.assertEqual([op for op, _ in store.ops], ["put", "put"])
        index = self.read_index(store)
        self.assertEqual(index["published_at"], "2026-09-30T12:05:00Z")
        self.assertEqual(next(e for e in index["pages"] if e["id"] == b), next(e for e in first["pages"] if e["id"] == b))
        self.assertEqual(index["assets"], first["assets"])
        self.assertEqual((store.dir / "o" / b).read_bytes(), b_blob)

        # B reads again: it is the very object that is still there, so nothing is sent for it.
        world.page("Notes/B.md", "<p>b</p>", text="b words")
        world.asset("/x/pic.png", b"pic")
        world.skipped = []
        store.ops.clear()
        report = self.publish(world, store, now=later)
        self.assertEqual((report.uploaded, report.deleted, report.index_uploaded), (0, 0, False))

    def test_mirror_deletes_nothing_on_a_truncated_build(self) -> None:
        world, store, keys = World(), RecordingStore(self.base / "out"), self.keys()
        self.publish_three(world, store)
        b = keys.ident("page:Notes/B.md")
        del world.items["page:Notes/B.md"]
        world.truncated = True
        store.ops.clear()
        self.publish(world, store)
        self.assertEqual(store.ops, [])
        self.assertIn("Notes/B.md", [e["path"] for e in self.read_index(store)["pages"]])

        # Changes still go up on a truncated build; only deleting waits.
        world.page("Notes/A.md", "<p>a, edited</p>", text="a, edited")
        store.ops.clear()
        self.assertEqual(self.publish(world, store).uploaded, 1)
        self.assertNotIn("delete", [op for op, _ in store.ops])
        self.assertIn("Notes/B.md", [e["path"] for e in self.read_index(store)["pages"]])

        # Nor does it matter that the last index is gone: missing is never read as deleted on a truncated build.
        sealed_index_path(self.data, store.destination).unlink()
        world.page("Notes/A.md", "<p>a, edited again</p>", text="a, edited again")
        store.ops.clear()
        self.publish(world, store)
        self.assertNotIn("delete", [op for op, _ in store.ops])
        self.assertTrue((store.dir / "o" / b).exists())

        world.truncated = False  # a complete walk that still lacks B: now it really is gone
        self.publish(world, store)
        self.assertNotIn("Notes/B.md", [e["path"] for e in self.read_index(store)["pages"]])
        self.assertFalse((store.dir / "o" / b).exists())

    def test_without_the_sealed_index_a_skipped_page_is_still_not_deleted(self) -> None:
        world, store, keys = World(), RecordingStore(self.base / "out"), self.keys()
        self.publish_three(world, store)
        b = keys.ident("page:Notes/B.md")
        sealed_index_path(self.data, store.destination).unlink()
        del world.items["page:Notes/B.md"]
        world.skipped = ["Notes/B.md"]
        world.page("Notes/A.md", "<p>a, edited</p>")
        self.publish(world, store)
        self.assertTrue((store.dir / "o" / b).exists())
        self.assertNotIn("Notes/B.md", [e["path"] for e in self.read_index(store)["pages"]])

    def test_the_sealed_index_kept_beside_the_state_is_ciphertext_and_wiped_with_it(self) -> None:
        world, store = World(), RecordingStore(self.base / "out")
        world.page("Notes/Quokka.md", "<p>quokka</p>", title="Quokka", text="quokka")
        self.publish(world, store)
        sealed = sealed_index_path(self.data, store.destination)
        self.assertEqual(stat.S_IMODE(sealed.stat().st_mode), 0o600)
        self.assertEqual(sealed.read_bytes(), (store.dir / "o" / self.keys().ident("index")).read_bytes())
        self.assertNotIn(b"Quokka", sealed.read_bytes())
        from onyx.mirror.publish import clear_state

        clear_state(self.data, store.destination)
        self.assertFalse(sealed.exists())
        self.assertFalse(state_path(self.data, store.destination).exists())

    def test_publishing_what_the_real_builder_builds_round_trips_and_settles(self) -> None:
        vault = self.base / "vault"
        (vault / "Areas").mkdir(parents=True)
        (vault / ".obsidian").mkdir()
        (vault / "Areas" / "Hello.md").write_text("# Hello\n\nSee [[World]] and ![pic](pic.png).\n", encoding="utf-8")
        (vault / "Areas" / "World.md").write_text("# World\n\nBack to [[Hello]].\n", encoding="utf-8")
        (vault / "Areas" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (vault / ".obsidian" / "x.md").write_text("# hidden\n", encoding="utf-8")
        (vault / "Elsewhere.md").write_text("# Not included\n", encoding="utf-8")
        config = mirror_config.MirrorConfig(True, 5, ("Notes/Areas",), ())
        store = RecordingStore(self.base / "out")
        keys = self.keys()

        def run() -> PublishReport:
            # The builder serves assets only from inside the home folder, which for this test is the temp dir.
            with patch("pathlib.Path.home", return_value=self.base):
                return publish(config=config, secrets=self.secrets, store=store, roots={"Notes": vault},
                               markdown_css=None, data_dir=self.data, now=NOW)

        report = run()
        self.assertEqual((report.pages, report.assets, report.uploaded), (2, 1, 3))
        index = self.read_index(store)
        self.assertEqual([p["path"] for p in index["pages"]], ["Notes/Areas/Hello.md", "Notes/Areas/World.md"])
        hello, world = index["pages"]
        page = keys.open_blob(hello["id"], (store.dir / "o" / hello["id"]).read_bytes()).decode("utf-8")
        self.assertIn(f'href="{world["id"]}"', page)  # a wikilink to a published page is that page's id
        self.assertIn(f'src="{index["assets"][0]["id"]}"', page)
        self.assertNotIn("Elsewhere", page + json.dumps(index))
        for entry in index["pages"]:
            self.assertEqual(hexsha((store.dir / "o" / entry["id"]).read_bytes()), entry["sha"])

        store.ops.clear()
        again = run()  # the builder's output is stable, so an unchanged vault sends nothing
        self.assertEqual((again.uploaded, again.index_uploaded, again.bytes), (0, False, 0))
        self.assertEqual(store.ops, [])

    def test_an_empty_build_does_not_erase_what_is_published(self) -> None:
        world = World()
        world.page("Notes/A.md", "<p>a</p>")
        store = RecordingStore(self.base / "out")
        self.publish(world, store)
        world.items.clear()  # the vault's drive dropped off and walked as empty
        store.ops.clear()
        with self.assertRaises(MirrorError):
            self.publish(world, store)
        self.assertEqual(store.ops, [])
        self.assertEqual(len(self.read_index(store)["pages"]), 1)

    def test_state_is_kept_per_destination_so_a_dry_run_never_poisons_the_real_bucket(self) -> None:
        world = World()
        world.page("Notes/A.md", "<p>a</p>")
        dry, real = RecordingStore(self.base / "dry"), RecordingStore(self.base / "real")
        self.publish(world, dry)
        report = self.publish(world, real)
        self.assertEqual(report.uploaded, 1)
        self.assertNotEqual(state_path(self.data, dry.destination), state_path(self.data, real.destination))
        self.assertEqual(stat.S_IMODE((self.data / "mirror").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state_path(self.data, real.destination).stat().st_mode), 0o600)
        # `status` counts only the real bucket's publishes, which a folder's never are.
        self.assertIsNone(last_published(self.data))

    def test_publishing_needs_the_master_key(self) -> None:
        with self.assertRaises(MirrorError) as caught:
            publish(config=CONFIG, secrets=MemorySecrets(), store=RecordingStore(self.base / "out"), roots={},
                    markdown_css=None, data_dir=self.data, build=World().build)
        self.assertIn("master_key", str(caught.exception))

    def test_one_publish_at_a_time(self) -> None:
        inside, got_it, release = threading.Event(), threading.Event(), threading.Event()

        def hold() -> None:
            with locked(self.data):
                inside.set()
                release.wait(5)

        def want() -> None:
            with locked(self.data):
                got_it.set()

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(inside.wait(5))
        waiter = threading.Thread(target=want)
        waiter.start()
        self.assertFalse(got_it.wait(0.3), "a second publish got in while the first held the lock")
        release.set()
        self.assertTrue(got_it.wait(5))
        holder.join()
        waiter.join()


# -- gate 5: credentials -------------------------------------------------------------------------------

class CredentialTests(MirrorTestCase):
    def sentinels(self) -> dict[str, str]:
        return {
            keychain.MASTER_KEY: generate_master(),
            keychain.READ_TOKEN: "ZSENTINEL-token-0001-abcdefghij",
            keychain.WORKER_URL: "https://zsentinel.example.workers.dev",
            keychain.R2_ACCOUNT_ID: "ZSENTINELACCT0001",
            keychain.R2_BUCKET: "zsentinel-bucket-0001",
            keychain.R2_ACCESS_KEY_ID: "ZSENTINELKEYID0001",
            keychain.R2_SECRET_ACCESS_KEY: "ZSENTINEL-secret-0001-abcdefghij",
        }

    def assertClean(self, text: str, values: dict[str, str], where: str) -> None:
        lowered = text.lower()
        for account, value in values.items():
            self.assertNotIn(value.lower(), lowered, f"{account} leaked into {where}")
        self.assertNotIn("zsentinel", lowered, f"a credential fragment leaked into {where}")

    def test_mirror_credentials_never_reach_settings_or_logs(self) -> None:
        values = self.sentinels()
        secrets = MemorySecrets()
        enable(self.data)

        # Setting them up writes the Keychain stand-in, the template, and nothing else.
        code, out, err = run_cli(["setup", "--from-stdin"], secrets=secrets, data_dir=self.data,
                                 stdin=json.dumps(values))
        self.assertEqual(code, 0, err)
        self.assertEqual({a: secrets.get(a) for a in keychain.ACCOUNTS}, values)
        code, status_out, status_err = run_cli(["status"], secrets=secrets, data_dir=self.data)
        self.assertEqual(code, 0)
        for text in (out, err, status_out, status_err):
            self.assertClean(text, values, "the CLI's output")
        self.assertIn("r2_secret_access_key: set", status_out)

        for shown in (repr(secrets), str(secrets), repr(keychain.KeychainSecrets()), repr(Keys(enc=b'\x01' * 32, id=b'\x02' * 32)),
                      repr(R2Store("a" * 32, "bucket-name", "AKIDVALUE1234", "secretvalue-0123456789")),
                      repr(PublishReport(1, 2, 3, 4, 5, 6, True))):
            self.assertClean(shown, values, "a repr")

        # The app's own surfaces: settings, diagnostics, its database and its data directory.
        config = AppConfig(default_folder=self.base, allowed_roots=(self.base,), data_dir=self.data)
        with TestClient(create_app(config), base_url="http://127.0.0.1:8899") as client:
            for url in ("/api/settings", "/api/diagnostics"):
                response = client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertClean(response.text, values, url)
        for path in self.data.rglob("*"):
            if path.is_file():
                self.assertClean(path.read_bytes().decode("latin-1"), values, f"{path.name} in the data directory")

        # A publish that fails three ways logs a type or our own words, never the library's.
        storage = app_storage.Storage(self.data)
        self.addCleanup(storage.close)
        host = f"{values[keychain.R2_ACCOUNT_ID]}.r2.cloudflarestorage.com"
        leaky = f"connect to {host} with {values[keychain.R2_SECRET_ACCESS_KEY]} at /Users/chris/Private"

        def failing_opener(error):
            def opener(request):
                raise error

            return opener

        attempts = [
            failing_opener(urllib.error.URLError(OSError(leaky))),
            failing_opener(http_error(self, 403, leaky, f"https://{host}/x")),
            failing_opener(TimeoutError(leaky)),
        ]
        with self.assertLogs("onyx.mirror", level="DEBUG") as logs:
            for opener in attempts:
                publisher = service.Publisher(storage, lambda: {"Notes": self.base}, secrets=secrets)
                store = R2Store.from_secrets(secrets, opener=opener)
                # Only the store's own failure is under test here, so the publish is reduced to its first request
                # (which also keeps this test independent of the optional cryptography extra).
                with patch.object(service.R2Store, "from_secrets", lambda *_a, **_k: store), \
                        patch.object(service, "publish", lambda **kw: kw["store"].put("ab" * 32, b"x")), \
                        patch.object(service, "markdown_css_for", lambda *_a: None):
                    publisher.cycle()
            boom = service.Publisher(storage, lambda: {"Notes": self.base}, secrets=secrets)

            def explode(**_kwargs):
                raise RuntimeError(leaky)

            with patch.object(service, "publish", explode), patch.object(service, "markdown_css_for", lambda *_a: None):
                boom.cycle()
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(len(messages), 4)
        self.assertClean("\n".join(messages + logs.output), values, "the log")
        self.assertIn("HTTP 403", "\n".join(messages))
        self.assertNotIn("Private", "\n".join(messages))
        self.assertClean(describe_error(RuntimeError(leaky)), values, "describe_error")
        self.assertEqual(describe_error(RuntimeError(leaky)), "RuntimeError")

    def test_a_repeated_failure_is_logged_once_and_a_recovery_resets_it(self) -> None:
        enable(self.data)
        storage = app_storage.Storage(self.data)
        self.addCleanup(storage.close)
        publisher = service.Publisher(storage, lambda: {}, secrets=MemorySecrets())
        with patch.object(service, "markdown_css_for", lambda *_a: None):
            with self.assertLogs("onyx.mirror", level="DEBUG") as logs:
                for _ in range(3):
                    publisher.cycle()  # the Keychain is empty, so each cycle fails the same way
        self.assertEqual(len(logs.records), 1)
        self.assertIn("r2_account_id", logs.output[0])

    def test_the_paused_publisher_follows_the_config_it_reads_each_cycle(self) -> None:
        storage = app_storage.Storage(self.data)
        self.addCleanup(storage.close)

        class Exploding(MemorySecrets):
            def get(self, account):
                raise AssertionError("the Keychain was read while the mirror was off")

        publisher = service.Publisher(storage, lambda: self.fail("roots read while off"), secrets=Exploding())
        self.assertEqual(publisher.cycle(), mirror_config.DEFAULT_INTERVAL_MINUTES)  # no file
        enable(self.data)
        mirror_config.disable(self.data)
        self.assertEqual(publisher.cycle(), mirror_config.DEFAULT_INTERVAL_MINUTES)  # switched off

    def test_keychain_writes_keep_values_off_argv(self) -> None:
        value = 'p@ss w"ord\\with#odd \'chars\' $HOME `x` ;'
        calls: list[tuple[list, dict]] = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(keychain.subprocess, "run", fake_run):
            keychain.KeychainSecrets().set(keychain.READ_TOKEN, value)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/usr/bin/security", "-i"])
        self.assertFalse(any(value in part or "ss w" in part for part in argv))
        self.assertEqual(
            kwargs["input"],
            'add-generic-password -U -s onyx-mirror -a read_token -w "p@ss w\\"ord\\\\with#odd \'chars\' $HOME `x` ;"\n',
        )
        # One command per write: a value can't smuggle in a second.
        self.assertEqual(kwargs["input"].count("\n"), 1)

        for bad in ("two\nlines", "carriage\rreturn", "", "café", "nul\x00byte", "tab\tbed"):
            with patch.object(keychain.subprocess, "run", fake_run):
                with self.assertRaises(SecretsError):
                    keychain.KeychainSecrets().set(keychain.READ_TOKEN, bad)
        with self.assertRaises(SecretsError):
            keychain.KeychainSecrets().set("not_an_account", "value")
        self.assertEqual(len(calls), 1, "a refused value never reached `security`")

    def test_keychain_reads_and_presence_checks(self) -> None:
        seen: list[list] = []

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            if "nope" in argv or keychain.MASTER_KEY in argv and "-w" not in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if keychain.READ_TOKEN in argv:
                return subprocess.CompletedProcess(argv, 44, stdout="", stderr="not found")
            return subprocess.CompletedProcess(argv, 0, stdout="the-value\n", stderr="")

        with patch.object(keychain.subprocess, "run", fake_run):
            secrets = keychain.KeychainSecrets()
            self.assertEqual(secrets.get(keychain.MASTER_KEY), "the-value")
            self.assertIsNone(secrets.get(keychain.READ_TOKEN))
            self.assertTrue(secrets.has(keychain.MASTER_KEY))
        self.assertEqual(seen[0], ["/usr/bin/security", "find-generic-password", "-s", "onyx-mirror",
                                   "-a", "master_key", "-w"])
        self.assertNotIn("-w", seen[2], "asking whether an item exists must not read its value")

    def test_keychain_failures_name_the_account_and_not_the_tool_output(self) -> None:
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="usage: ... -w hunter2hunter2")

        with patch.object(keychain.subprocess, "run", fake_run):
            with self.assertRaises(SecretsError) as caught:
                keychain.KeychainSecrets().set(keychain.R2_BUCKET, "hunter2hunter2")
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertIn("r2_bucket", str(caught.exception))


# -- the command line ----------------------------------------------------------------------------------

def valid_values() -> dict[str, str]:
    return {
        keychain.WORKER_URL: "https://onyx-mirror.example.workers.dev/",
        keychain.R2_ACCOUNT_ID: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        keychain.R2_BUCKET: "onyx-mirror-test",
        keychain.R2_ACCESS_KEY_ID: "AKIDEXAMPLE0001",
        keychain.R2_SECRET_ACCESS_KEY: "example-secret-0123456789",
    }


class CliTests(MirrorTestCase):
    def test_setup_generates_the_keys_once_and_never_prints_a_value(self) -> None:
        secrets = MemorySecrets()
        values = valid_values()
        code, out, err = run_cli(["setup", "--generate", "--from-stdin"], secrets=secrets, data_dir=self.data,
                                 stdin=json.dumps(values))
        self.assertEqual((code, err), (0, ""))
        master, token = secrets.get(keychain.MASTER_KEY), secrets.get(keychain.READ_TOKEN)
        self.assertEqual(len(b64url_decode(master)), 32)
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(secrets.get(keychain.WORKER_URL), "https://onyx-mirror.example.workers.dev")  # no trailing slash
        for value in (master, token, *values.values()):
            self.assertNotIn(value, out + err)
        self.assertIn("Generated master_key.", out)
        self.assertIn("Generated read_token.", out)

        # The template it writes is off, names nothing, and holds no credential.
        template = (self.data / "mirror.toml").read_text(encoding="utf-8")
        self.assertIn("enabled = false", template)
        self.assertIsNone(mirror_config.load(self.data))
        self.assertEqual(stat.S_IMODE((self.data / "mirror.toml").stat().st_mode), 0o600)

        # Running it again keeps the keys it made, and leaves an existing mirror.toml alone.
        enable(self.data, '["Notes/Mine"]')
        code, out, _ = run_cli(["setup", "--generate", "--from-stdin"], secrets=secrets, data_dir=self.data, stdin="{}")
        self.assertEqual((secrets.get(keychain.MASTER_KEY), secrets.get(keychain.READ_TOKEN)), (master, token))
        self.assertNotIn("Generated", out)
        self.assertEqual(mirror_config.load(self.data).include, ("Notes/Mine",))

    def test_setup_refuses_bad_input_without_storing_or_echoing_it(self) -> None:
        secret = "hunter2-very-secret-value"
        cases = [
            ("not json " + secret, "not JSON"),
            (json.dumps([secret]), "one JSON object"),
            (json.dumps({keychain.R2_BUCKET: 7}), "one JSON object"),
            (json.dumps({"nonsense_" + secret: "x"}), "account"),
        ]
        for stdin, expected in cases:
            secrets = MemorySecrets()
            code, out, err = run_cli(["setup", "--from-stdin"], secrets=secrets, data_dir=self.data, stdin=stdin)
            self.assertEqual(code, 2, stdin)
            self.assertIn(expected, err)
            self.assertNotIn("hunter2", out + err)
            self.assertEqual(secrets._values, {})
        for account, bad in ((keychain.WORKER_URL, "http://" + secret), (keychain.MASTER_KEY, secret),
                             (keychain.R2_ACCOUNT_ID, secret), (keychain.R2_BUCKET, secret.upper()),
                             (keychain.READ_TOKEN, "short"), (keychain.R2_SECRET_ACCESS_KEY, "has space " * 3)):
            secrets = MemorySecrets()
            good = valid_values()
            code, out, err = run_cli(["setup", "--from-stdin"], secrets=secrets, data_dir=self.data,
                                     stdin=json.dumps({**good, account: bad}))
            self.assertEqual(code, 2, account)
            self.assertIn(account, err)
            self.assertNotIn("hunter2", out + err)
            self.assertEqual(secrets._values, {}, "one bad value must not leave the others half saved")

    def test_setup_prompts_without_echo_when_stdin_is_not_used(self) -> None:
        secrets = MemorySecrets()
        answers = {"master_key": "", "worker_url": "https://w.example.workers.dev", "r2_bucket": "onyx-mirror-test"}
        asked: list[str] = []

        def fake_getpass(prompt: str) -> str:
            asked.append(prompt)
            return answers.get(prompt.split(" ")[0].rstrip(":"), "")

        with patch("getpass.getpass", fake_getpass):
            code, out, err = run_cli(["setup"], secrets=secrets, data_dir=self.data)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(asked), len(keychain.ACCOUNTS))
        self.assertEqual(secrets._values, {"worker_url": "https://w.example.workers.dev", "r2_bucket": "onyx-mirror-test"})
        self.assertIn("Still missing:", out)
        self.assertNotIn("onyx-mirror-test", out)

    def test_replacing_the_master_key_says_what_follows(self) -> None:
        secrets = MemorySecrets({keychain.MASTER_KEY: generate_master()})
        code, out, _ = run_cli(["setup", "--from-stdin"], secrets=secrets, data_dir=self.data,
                               stdin=json.dumps({keychain.MASTER_KEY: generate_master()}))
        self.assertEqual(code, 0)
        self.assertIn("pair them again", out)

    def pairing_secrets(self) -> MemorySecrets:
        return MemorySecrets({keychain.MASTER_KEY: VECTORS["master_key_b64url"], keychain.READ_TOKEN: "t" * 40,
                              keychain.WORKER_URL: "https://mirror.example.workers.dev"})

    def test_pair_copy_hands_the_string_to_the_clipboard_and_not_the_terminal(self) -> None:
        secrets = self.pairing_secrets()
        expected = cli.pairing_string(secrets)
        calls = []
        with patch.object(cli.subprocess, "run", lambda argv, **kw: calls.append((list(argv), kw)) or
                          subprocess.CompletedProcess(argv, 0)):
            code, out, err = run_cli(["pair", "--copy"], secrets=secrets, data_dir=self.data)
        self.assertEqual((code, out, err), (0, "Copied.\n", ""))
        (argv, kwargs), = calls
        self.assertEqual(argv, ["/usr/bin/pbcopy"])
        self.assertEqual(kwargs["input"], expected)

    @needs_extra
    def test_pair_shows_a_qr_in_preview_from_a_private_folder_and_schedules_its_deletion(self) -> None:
        import segno

        secrets = self.pairing_secrets()
        text = cli.pairing_string(secrets)
        seen: dict = {}

        def fake_run(argv, **kwargs):
            path = Path(argv[1])
            seen.update(argv=list(argv), mode=stat.S_IMODE(path.parent.stat().st_mode), png=path.read_bytes(),
                        folder=path.parent)
            return subprocess.CompletedProcess(argv, 0)

        popen = MagicMock()
        with patch.object(cli.subprocess, "run", fake_run), patch.object(cli.subprocess, "Popen", popen):
            code, out, err = run_cli(["pair"], secrets=secrets, data_dir=self.data)
        self.addCleanup(lambda: __import__("shutil").rmtree(seen["folder"], ignore_errors=True))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "QR opened in Preview; it's deleted in 2 minutes.\n")
        self.assertNotIn(text, out)
        self.assertEqual(seen["argv"][0], "/usr/bin/open")
        self.assertEqual(seen["mode"], 0o700)
        self.assertEqual(seen["png"][:8], b"\x89PNG\r\n\x1a\n")
        expected = io.BytesIO()
        segno.make(text, error="m").save(expected, kind="png", scale=8, border=4)
        self.assertEqual(seen["png"], expected.getvalue(), "the QR code must encode the pairing string")
        (args, kwargs), = popen.call_args_list
        self.assertEqual(args[0][-2:], ["120", str(seen["folder"])])
        self.assertTrue(kwargs["start_new_session"], "the deletion has to outlive this command")

    @needs_extra
    def test_a_qr_that_will_not_open_is_deleted_at_once(self) -> None:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["folder"] = Path(argv[1]).parent
            return subprocess.CompletedProcess(argv, 1)

        popen = MagicMock()
        with patch.object(cli.subprocess, "run", fake_run), patch.object(cli.subprocess, "Popen", popen):
            code, out, err = run_cli(["pair"], secrets=self.pairing_secrets(), data_dir=self.data)
        self.assertEqual(code, 1)
        self.assertFalse(seen["folder"].exists())
        popen.assert_not_called()

    @needs_extra
    def test_pair_terminal_draws_the_code_and_never_the_string(self) -> None:
        secrets = self.pairing_secrets()
        code, out, err = run_cli(["pair", "--terminal"], secrets=secrets, data_dir=self.data)
        self.assertEqual(code, 0, err)
        self.assertGreater(len(out.splitlines()), 10)
        self.assertNotIn("onyxmirror1", out)

    def test_pair_names_what_is_missing_and_shows_nothing(self) -> None:
        secrets = MemorySecrets({keychain.MASTER_KEY: VECTORS["master_key_b64url"]})
        for flags in ([], ["--copy"], ["--terminal"]):
            code, out, err = run_cli(["pair", *flags], secrets=secrets, data_dir=self.data)
            self.assertEqual(code, 2)
            self.assertIn("read_token, worker_url", err)
            self.assertEqual(out, "")

    def test_stop_switches_publishing_off_and_status_says_so(self) -> None:
        enable(self.data, '["Notes/A"]')
        code, out, _ = run_cli(["status"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertIn("Phone mirror: on, every 5 min", out)
        self.assertIn("include: Notes/A", out)
        self.assertIn("master_key: missing", out)
        code, out, _ = run_cli(["stop"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertEqual(code, 0)
        self.assertIsNone(mirror_config.load(self.data))
        code, out, _ = run_cli(["status"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertIn("Phone mirror: off", out)
        (self.data / "mirror.toml").unlink()
        code, out, _ = run_cli(["status"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertIn("not set up", out)
        self.assertFalse((self.data / "mirror").exists(), "status must not create anything")
        self.assertIn("no mirror.toml", run_cli(["stop"], secrets=MemorySecrets(), data_dir=self.data)[1])

    def test_status_reports_the_last_real_publish(self) -> None:
        path = state_path(self.data, "r2-0123456789ab")
        path.write_text(json.dumps({"v": 1, "objects": {}, "index": None, "last": {
            "at": "2026-09-30T12:00:00Z", "pages": 4, "assets": 2, "uploaded": 3, "unchanged": 3, "deleted": 1,
            "bytes": 999}}), encoding="utf-8")
        code, out, _ = run_cli(["status"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertIn("Last publish: 2026-09-30T12:00:00Z, 4 pages and 2 assets (3 uploaded, 1 deleted, 999 bytes)", out)

    def test_wipe_asks_first_then_empties_the_bucket_and_forgets_it_did(self) -> None:
        class Bucket:
            destination = "r2-fakefakefake"

            def __init__(self) -> None:
                self.ids = ["a" * 64, "b" * 64]
                self.deleted: list[str] = []

            def list_ids(self):
                return list(self.ids)

            def delete(self, id):
                self.deleted.append(id)

        bucket = Bucket()
        state = state_path(self.data, bucket.destination)
        state.write_text("{}", encoding="utf-8")
        with patch.object(cli, "_r2", lambda _s: bucket):
            with patch("builtins.input", return_value="yes please"):
                code, out, _ = run_cli(["wipe"], secrets=MemorySecrets(), data_dir=self.data)
            self.assertEqual((code, bucket.deleted), (1, []))
            self.assertTrue(state.exists())
            enable(self.data)
            with patch("builtins.input", side_effect=AssertionError("--yes must not ask")):
                code, out, _ = run_cli(["wipe", "--yes"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertEqual(code, 0)
        self.assertEqual(bucket.deleted, bucket.ids)
        self.assertFalse(state.exists())
        self.assertIn("Deleted 2 objects", out)
        self.assertIn("still on", out)

    @needs_extra
    def test_publish_to_a_folder_is_a_dry_run_that_never_builds_an_r2_client(self) -> None:
        enable(self.data)
        world = World()
        world.page("Notes/A.md", "<p>a</p>")

        def boom(*_a, **_k):
            raise AssertionError("a dry run must not build the bucket client")

        with patch.object(cli, "publish", lambda **kw: publish(**kw, build=world.build)), \
                patch.object(service, "roots_from_settings", lambda _s: {"Notes": self.base}), \
                patch.object(service, "markdown_css_for", lambda *_a: None), \
                patch.object(cli, "R2Store", boom):
            code, out, err = run_cli(["publish", "--to", str(self.base / "dry")], secrets=self.secrets,
                                     data_dir=self.data)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Published 1 pages and 0 assets: 1 uploaded", out)
        self.assertIn("dry run", out)
        self.assertEqual(len(list((self.base / "dry" / "o").iterdir())), 3)  # the page, the index, the library

    def test_the_cli_opens_the_app_storage_as_the_app_does(self) -> None:
        # Storage(None) adopts a pre-rename database; Storage(<default dir>) would create an empty one first.
        with patch.object(app_storage, "Storage") as storage_class:
            cli._open_storage(app_storage.default_data_dir())
            storage_class.assert_called_once_with(None)
            storage_class.reset_mock()
            cli._open_storage(self.data)
            storage_class.assert_called_once_with(self.data)

    def test_onyx_mirror_dispatches_before_the_server_parses_its_flags(self) -> None:
        from onyx import __main__ as entry

        with patch.object(sys, "argv", ["onyx", "mirror", "status"]), \
                patch("onyx.mirror.cli.main", return_value=0) as mirror_main:
            with self.assertRaises(SystemExit) as caught:
                entry.main()
        mirror_main.assert_called_once_with(["status"])
        self.assertEqual(caught.exception.code, 0)

    def test_an_unexpected_error_shows_its_type_and_not_its_message(self) -> None:
        enable(self.data)
        with patch.object(cli, "_r2", side_effect=RuntimeError("https://example-acct.r2.cloudflarestorage.com secret")):
            code, out, err = run_cli(["publish"], secrets=MemorySecrets(), data_dir=self.data)
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("cloudflarestorage", out + err)


# -- the look: the phone wears what the Mac app wears ---------------------------------------------------

LOOK_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "mirror_look_vectors.json").read_text(encoding="utf-8"))
# A reading-view and explorer snapshot, in the shape the Obsidian plugin posts (markdown_theme / sidebar_theme).
MARKDOWN_SNAPSHOT = {"mode": "light", "styles": {"content": {"background-color": "rgb(253, 246, 227)",
                                                             "color": "rgb(0, 43, 54)"},
                                                 "a": {"color": "rgb(203, 75, 22)"}}}
SIDEBAR_SNAPSHOT = {"mode": "light", "styles": {"pane": {"background-color": "rgb(238, 232, 213)",
                                                         "color": "rgb(0, 43, 54)",
                                                         "font-family": '"JetBrains Mono", sans-serif'}}}


class FakeThemeStorage:
    """The three Storage calls look_for makes, by their real names: with a ``mode``, the snapshot of that colour
    mode (Obsidian's own, or the other one the plugin measured), as Storage._theme answers."""

    def __init__(self, settings: dict, markdown=MARKDOWN_SNAPSHOT, sidebar=SIDEBAR_SNAPSHOT,
                 other_markdown=None, other_sidebar=None) -> None:
        self._settings = settings
        self._markdown, self._sidebar = (markdown, other_markdown), (sidebar, other_sidebar)

    def settings(self, **_) -> dict:
        return dict(self._settings)

    @staticmethod
    def _pick(pair, mode):
        current, other = pair
        if mode is None or (current or {}).get("mode") == mode:
            return current
        return other if (other or {}).get("mode") == mode else None

    def markdown_theme(self, root, mode=None):
        return self._pick(self._markdown, mode)

    def sidebar_theme(self, root, mode=None):
        return self._pick(self._sidebar, mode)


class LookTests(MirrorTestCase):
    def test_the_page_look_vectors_are_what_the_mac_computes(self) -> None:
        # The iOS app ports palette() and must reproduce this file, so it has to stay what the Mac actually computes.
        from onyx import vault_look

        self.assertEqual(LOOK_VECTORS["onyx_blue"], vault_look.ONYX_BLUE)
        for case in LOOK_VECTORS["cases"]:
            computed = vault_look.palette({"styles": {
                "content": {"background-color": case["background"], "color": case["ink"]},
                "a": {"color": case["link"] or vault_look.ONYX_BLUE},
            }}, None)
            self.assertEqual(computed, case["look"], case["name"])

    def test_the_phone_gets_the_vault_look_only_while_the_mac_wears_it(self) -> None:
        from onyx import vault_look

        roots = {"Notes": self.base}
        on = service.look_for(FakeThemeStorage({"appearance_theme": "dark"}), roots)
        self.assertEqual(on["vault"], vault_look.palette(MARKDOWN_SNAPSHOT, SIDEBAR_SNAPSHOT))
        self.assertEqual(on["vault"]["tokens"]["--ui-font"], '"JetBrains Mono", sans-serif')
        self.assertEqual((on["follow_page"], on["appearance"]), (True, "dark"))

        off = service.look_for(FakeThemeStorage({"markdown_follow_obsidian": False, "sidebar_follow_obsidian": False,
                                                 "html_follow_page": False, "appearance_theme": "bogus"}), roots)
        self.assertEqual(off, {"vault": None, "vaults": None, "follow_page": False, "appearance": "system"})
        # Either switch keeps it on, as current_vault_look reads them.
        one = service.look_for(FakeThemeStorage({"markdown_follow_obsidian": False}), roots)
        self.assertIsNotNone(one["vault"])
        self.assertIsNone(service.look_for(FakeThemeStorage({}), {})["vault"])  # no notes vault, nothing to wear

    def test_the_phone_gets_each_mode_the_plugin_measured_and_the_one_the_mac_wears(self) -> None:
        from onyx import vault_look

        roots = {"Notes": self.base}
        dark_md = {"mode": "dark", "styles": {"content": {"background-color": "rgb(0, 43, 54)", "color": "rgb(238, 232, 213)"}}}
        dark_sb = {"mode": "dark", "styles": {"pane": {"background-color": "rgb(7, 54, 66)", "color": "rgb(238, 232, 213)"}}}
        light = vault_look.palette(MARKDOWN_SNAPSHOT, SIDEBAR_SNAPSHOT)
        dark = vault_look.palette(dark_md, dark_sb)
        both = service.look_for(FakeThemeStorage({}, other_markdown=dark_md, other_sidebar=dark_sb), roots)
        self.assertEqual(both["vaults"], {"light": light, "dark": dark})
        self.assertEqual(both["vault"], light, "the Mac's Color theme: Same as Obsidian")
        chosen = service.look_for(FakeThemeStorage({"vault_mode": "dark"}, other_markdown=dark_md, other_sidebar=dark_sb), roots)
        self.assertEqual((chosen["vault"], chosen["vaults"]["light"]), (dark, light))
        # An older plugin: only Obsidian's own mode, and the other is null rather than a guess.
        self.assertEqual(service.look_for(FakeThemeStorage({}), roots)["vaults"], {"light": light, "dark": None})

    @needs_extra
    def test_a_theme_change_republishes_only_the_index(self) -> None:
        world, store = World(), RecordingStore(self.base / "out")
        world.page("Notes/a.md", "<p>a</p>")
        look = {"vault": {"mode": "light", "base": [1, 2, 3], "tokens": {"--ink": "0 0 0"}},
                "follow_page": True, "appearance": "system"}

        def run(look_value) -> None:
            publish(config=CONFIG, secrets=self.secrets, store=store, roots={"Notes": self.base},
                    markdown_css=None, data_dir=self.data, build=world.build, now=NOW, look=look_value)

        run(look)
        self.assertEqual(self.read_index(store)["look"], look)
        index_id = self.keys().ident("index")
        store.ops.clear()
        run(look)
        self.assertEqual(store.ops, [], "an unchanged look must not re-send the index")
        run({**look, "vault": {**look["vault"], "mode": "dark"}})
        self.assertEqual(store.ops, [("put", index_id)], "a changed look re-sends the index and nothing else")
        self.assertEqual(self.read_index(store)["look"]["vault"]["mode"], "dark")


# -- the signer and the R2 client ----------------------------------------------------------------------

AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_DATE = "20130524T000000Z"
EMPTY_SHA = hexsha(b"")


class SignerTests(unittest.TestCase):
    def sign(self, method, path, query, headers, payload_hash=EMPTY_SHA) -> str:
        return sign_v4(method=method, path=path, query=query, headers=headers, payload_hash=payload_hash,
                       access_key_id=AWS_KEY, secret_access_key=AWS_SECRET, amz_date=AWS_DATE, region="us-east-1")

    def test_sigv4_signer_matches_awss_published_examples(self) -> None:
        # "Signature Calculations for the Authorization Header" in the S3 API reference.
        host = "examplebucket.s3.amazonaws.com"
        get = {"host": host, "range": "bytes=0-9", "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": AWS_DATE}
        self.assertTrue(self.sign("GET", "/test.txt", [], get).endswith(
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

        body = b"Welcome to Amazon S3."
        put = {"date": "Fri, 24 May 2013 00:00:00 GMT", "host": host, "x-amz-content-sha256": hexsha(body),
               "x-amz-date": AWS_DATE, "x-amz-storage-class": "REDUCED_REDUNDANCY"}
        signed = self.sign("PUT", "/test$file.text", [], put, hexsha(body))
        self.assertTrue(signed.endswith("Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"))
        self.assertTrue(signed.startswith(f"AWS4-HMAC-SHA256 Credential={AWS_KEY}/20130524/us-east-1/s3/aws4_request, "))

        plain = {"host": host, "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": AWS_DATE}
        self.assertTrue(self.sign("GET", "/", [("lifecycle", "")], plain).endswith(
            "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"))
        self.assertTrue(self.sign("GET", "/", [("prefix", "J"), ("max-keys", "2")], plain).endswith(
            "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))

    def test_canonical_request_is_built_the_way_aws_defines_it(self) -> None:
        request, signed = canonical_request(
            "PUT", "/bucket/o/abc def",
            [("prefix", "o/"), ("a b", "~x")],
            {"Host": "h.example", "X-Amz-Date": AWS_DATE, "Content-Type": "application/octet-stream", "X-Note": "  a   b "},
            "deadbeef",
        )
        self.assertEqual(signed, "content-type;host;x-amz-date;x-note")
        self.assertEqual(request, "\n".join([
            "PUT",
            "/bucket/o/abc%20def",
            "a%20b=~x&prefix=o%2F",
            "content-type:application/octet-stream\nhost:h.example\nx-amz-date:20130524T000000Z\nx-note:a b\n",
            "content-type;host;x-amz-date;x-note",
            "deadbeef",
        ]))


class FakeResponse:
    def __init__(self, body: bytes = b"") -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


class R2StoreTests(unittest.TestCase):
    ACCOUNT, BUCKET, ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90", "onyx-mirror-test", "ab" * 32

    def store(self, responses, **kwargs) -> tuple[R2Store, list]:
        requests: list = []
        queue = list(responses)

        def opener(request):
            requests.append(request)
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, BaseException):
                raise item
            return FakeResponse(item)

        clock = lambda: datetime(2026, 9, 30, 12, 0, 5, tzinfo=timezone.utc)  # noqa: E731
        return R2Store(self.ACCOUNT, self.BUCKET, "AKIDEXAMPLE0001", "example-secret-0123456789",
                       opener=opener, clock=clock, **kwargs), requests

    def test_a_put_is_a_signed_path_style_request_with_the_payload_hash(self) -> None:
        store, requests = self.store([b""])
        store.put(self.ID, b"sealed bytes")
        (request,) = requests
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, f"https://{self.ACCOUNT}.r2.cloudflarestorage.com/{self.BUCKET}/o/{self.ID}")
        self.assertEqual(request.data, b"sealed bytes")
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["x-amz-content-sha256"], hexsha(b"sealed bytes"))
        self.assertEqual(headers["x-amz-date"], "20260930T120005Z")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertRegex(
            headers["authorization"],
            r"^AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE0001/20260930/auto/s3/aws4_request, "
            r"SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$",
        )
        expected = sign_v4(
            method="PUT", path=f"/{self.BUCKET}/o/{self.ID}", query=[],
            headers={"host": f"{self.ACCOUNT}.r2.cloudflarestorage.com", "x-amz-content-sha256": hexsha(b"sealed bytes"),
                     "x-amz-date": "20260930T120005Z", "content-type": "application/octet-stream"},
            payload_hash=hexsha(b"sealed bytes"), access_key_id="AKIDEXAMPLE0001",
            secret_access_key="example-secret-0123456789", amz_date="20260930T120005Z",
        )
        self.assertEqual(headers["authorization"], expected)

    def test_delete_treats_a_missing_object_as_done(self) -> None:
        store, requests = self.store([http_error(self, 404, "no")])
        store.delete(self.ID)
        self.assertEqual(requests[0].get_method(), "DELETE")
        store, _ = self.store([http_error(self, 403, "no")])
        with self.assertRaises(StoreError) as caught:
            store.delete(self.ID)
        self.assertEqual(str(caught.exception), "R2 DELETE failed: HTTP 403")

    def test_listing_follows_continuation_tokens_and_keeps_only_object_ids(self) -> None:
        ns = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
        first = (f'<?xml version="1.0"?><ListBucketResult {ns}><IsTruncated>true</IsTruncated>'
                 f"<NextContinuationToken>tok/+=1</NextContinuationToken>"
                 f"<Contents><Key>o/{'1' * 64}</Key></Contents><Contents><Key>o/not-an-id</Key></Contents>"
                 f"<Contents><Key>elsewhere/{'2' * 64}</Key></Contents></ListBucketResult>").encode()
        second = (f'<?xml version="1.0"?><ListBucketResult {ns}><IsTruncated>false</IsTruncated>'
                  f"<Contents><Key>o/{'3' * 64}</Key></Contents></ListBucketResult>").encode()
        store, requests = self.store([first, second])
        self.assertEqual(store.list_ids(), ["1" * 64, "3" * 64])
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0].full_url.endswith(f"/{self.BUCKET}?list-type=2&prefix=o%2F"))
        self.assertTrue(requests[1].full_url.endswith("?continuation-token=tok%2F%2B%3D1&list-type=2&prefix=o%2F"))

    def test_errors_carry_a_status_or_a_type_and_never_the_endpoint(self) -> None:
        endpoint_text = f"{self.ACCOUNT}.r2.cloudflarestorage.com example-secret-0123456789"
        cases = [
            (http_error(self, 500, endpoint_text, f"https://{endpoint_text}"), "R2 PUT failed: HTTP 500"),
            (urllib.error.URLError(ConnectionResetError(endpoint_text)), "R2 PUT failed: ConnectionResetError"),
            (TimeoutError(endpoint_text), "R2 PUT failed: TimeoutError"),
        ]
        for error, message in cases:
            store, _ = self.store([error])
            with self.assertRaises(StoreError) as caught:
                store.put(self.ID, b"x")
            self.assertEqual(str(caught.exception), message)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__, "the library's error must not ride along")

    def test_what_becomes_a_hostname_or_a_path_is_checked(self) -> None:
        for account in ("evil.com/x", "a" * 7, "a b c d e f g h", "abc.def.ghi.jkl", ""):
            with self.assertRaises(MirrorError):
                R2Store(account, self.BUCKET, "k", "s")
        for bucket in ("a/../b", "UPPER", "x", "has space", ".lead", "trail-"):
            with self.assertRaises(MirrorError):
                R2Store(self.ACCOUNT, bucket, "k", "s")
        store, _ = self.store([b""])
        for bad in ("../x", "short", "AB" * 32, self.ID + "0"):
            with self.assertRaises(StoreError):
                store.put(bad, b"x")

    def test_local_dir_store_refuses_ids_that_could_leave_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalDirStore(Path(tmp))
            for bad in ("../escape", "/etc/passwd", "a" * 63, "A" * 64):
                with self.assertRaises(StoreError):
                    store.put(bad, b"x")
            store.put(self.ID, b"x")
            self.assertEqual(store.list_ids(), [self.ID])
            store.delete(self.ID)
            store.delete(self.ID)  # already gone is fine
            self.assertEqual(store.list_ids(), [])


# -- gate 8: nothing private in the repository ---------------------------------------------------------

_PLACEHOLDER = re.compile(r"example|your[-_ ]|placeholder|xxxx|[<>{}$]", re.IGNORECASE)
_HOSTS = (
    ("a workers.dev host", re.compile(r"([\w<>{}.$-]+)\.workers\.dev", re.IGNORECASE)),
    ("an R2 account endpoint", re.compile(r"([\w<>{}$-]+)\.r2\.cloudflarestorage\.com", re.IGNORECASE)),
    ("a bearer token", re.compile(r"\bbearer\s+([A-Za-z0-9\-._~+/]{20,}=*)", re.IGNORECASE)),
    ("an AWS access key id", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
)


def private_looking(text: str) -> list[str]:
    """Kinds of private-looking thing in ``text``: a real host, endpoint or token rather than a placeholder."""
    found = []
    for kind, pattern in _HOSTS:
        for match in pattern.finditer(text):
            if not _PLACEHOLDER.search(match.group(0)):
                found.append(kind)
    return found


class RepoScanTests(unittest.TestCase):
    def tracked(self) -> list[Path]:
        try:
            done = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available here")
        return [ROOT / name for name in done.stdout.decode("utf-8").split("\0") if name]

    def test_repo_tracks_no_private_endpoint(self) -> None:
        offenders = []
        for path in self.tracked():
            try:
                if not path.is_file() or path.stat().st_size > 2_000_000:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8000]:
                continue
            for kind in private_looking(raw.decode("utf-8", errors="ignore")):
                offenders.append(f"{path.relative_to(ROOT)}: {kind}")
        self.assertEqual(offenders, [], "a tracked file holds what looks like a private endpoint or token")

    def test_the_private_endpoint_scan_catches_what_it_should_and_passes_placeholders(self) -> None:
        # Built at run time so this file holds nothing the scan would flag.
        real_worker = "onyx-mirror.chris-r." + "workers" + ".dev"
        real_r2 = "0123456789abcdef0123456789abcdef." + "r2.cloudflarestorage" + ".com"
        real_bearer = "Authorization: Bear" + "er " + "q8Zk3vN1xT7mB4pLr2WcY9sHd6JfGa5E"
        real_aws = "AKIA" + "Q7ZK3VN1XT7MB4PL"
        for text, kind in ((real_worker, "a workers.dev host"), (real_r2, "an R2 account endpoint"),
                           (real_bearer, "a bearer token"), (real_aws, "an AWS access key id")):
            self.assertIn(kind, private_looking(f"see https://{text} here"), text)
        for placeholder in (
            "https://mirror.example.workers.dev",
            "https://<your-subdomain>.workers.dev",
            "https://<account_id>.r2.cloudflarestorage.com",
            "Authorization: Bearer <read token>",
            "Authorization: Bearer $READ_TOKEN",
            "Authorization: Bearer example-token-aaaaaaaaaaaaaaaa",
            AWS_KEY,
        ):
            self.assertEqual(private_looking(placeholder), [], placeholder)


if __name__ == "__main__":
    unittest.main()
