from __future__ import annotations

import base64
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LAUNCHER = ROOT / "launcher"

# One appcast item, the shape scripts/release.sh writes. Only the enclosure varies.
APPCAST = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Onyx</title>
    <item>
      <title>Onyx 9.9.9</title>
      <sparkle:version>9.9.9</sparkle:version>
      <sparkle:shortVersionString>9.9.9</sparkle:shortVersionString>
      <enclosure url="{url}" length="{length}" type="application/octet-stream" sparkle:edSignature="{signature}" />
    </item>
  </channel>
</rss>
"""


def _plist() -> dict:
    with (LAUNCHER / "Info.plist").open("rb") as handle:
        return plistlib.load(handle)


class UpdaterConfigTests(unittest.TestCase):
    def test_the_app_reads_a_github_https_feed_and_asks_before_installing(self) -> None:
        plist = _plist()
        feed = urlparse(plist["SUFeedURL"])

        self.assertEqual(feed.scheme, "https")
        self.assertEqual(feed.netloc, "github.com")
        self.assertTrue(feed.path.startswith("/cxrobx/onyx/releases/"), feed.path)
        self.assertTrue(feed.path.endswith("/appcast.xml"), feed.path)
        self.assertIs(plist["SUEnableAutomaticChecks"], True)
        self.assertEqual(plist["SUScheduledCheckInterval"], 86400)
        # The user is prompted: nothing installs on its own.
        self.assertIs(plist["SUAutomaticallyUpdate"], False)
        # An archive is checked against its signature before it is unpacked.
        self.assertIs(plist["SUVerifyUpdateBeforeExtraction"], True)

    def test_the_app_carries_an_ed25519_public_key(self) -> None:
        key = _plist()["SUPublicEDKey"]

        self.assertTrue(key.strip())
        self.assertEqual(len(base64.b64decode(key, validate=True)), 32)

    def test_the_launcher_updates_with_sparkle_and_keeps_no_checker_of_its_own(self) -> None:
        swift = (LAUNCHER / "Onyx.swift").read_text(encoding="utf-8")

        self.assertIn("SPUStandardUpdaterController", swift)
        self.assertIn("Check for Updates…", swift)
        # The hand-rolled check asked GitHub's API for the latest tag and offered the release page.
        self.assertNotIn("api.github.com", swift)
        self.assertNotIn("releasesAPIURL", swift)

    def test_sparkle_is_a_pinned_checksummed_download_and_is_never_committed(self) -> None:
        script = (LAUNCHER / "fetch-sparkle.sh").read_text(encoding="utf-8")

        self.assertRegex(script, r'SPARKLE_VERSION="\d+\.\d+\.\d+"')
        self.assertRegex(script, r'SPARKLE_SHA256="[0-9a-f]{64}"')
        self.assertIn("releases/download/$SPARKLE_VERSION/", script)
        if shutil.which("git") and (ROOT / ".git").exists():
            tracked = subprocess.run(
                ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout.splitlines()
            self.assertEqual([path for path in tracked if "Sparkle.framework" in path], [])

    def test_the_build_embeds_and_signs_sparkle_inside_out(self) -> None:
        build = (LAUNCHER / "build-app.sh").read_text(encoding="utf-8")
        release = build[build.index('echo "→ Signing…"') : build.index("else\n  # macOS keeps a permission")]
        order = [
            release.index(part)
            for part in (
                "XPCServices/Installer.xpc",
                "XPCServices/Downloader.xpc",
                '$SPARKLE_FW/Autoupdate"',
                "$SPARKLE_FW/Updater.app",
                '"$BUNDLE/Contents/Frameworks/Sparkle.framework"',
                "Contents/Resources/Server",  # the frozen service's own binaries
                '--sign "$SIGN_IDENTITY" "$BUNDLE"',  # the bundle last
            )
        ]

        self.assertEqual(order, sorted(order), "nested code is signed before what contains it")
        self.assertIn("-Xlinker -rpath -Xlinker @executable_path/../Frameworks", build)
        self.assertIn("--preserve-metadata=entitlements", release)
        # Sparkle's documentation signs its helpers one by one; --deep is for the unsigned local builds only.
        self.assertNotIn("--deep ", release.replace("--deep never", ""))


@unittest.skipUnless(sys.platform == "darwin", "the release scripts are macOS tooling")
class ReleaseScriptTests(unittest.TestCase):
    def _run(self, *command: str) -> subprocess.CompletedProcess:
        return subprocess.run(list(command), cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_every_script_parses(self) -> None:
        for script in (
            SCRIPTS / "release.sh",
            SCRIPTS / "verify-update-feed.sh",
            LAUNCHER / "fetch-sparkle.sh",
            LAUNCHER / "build-app.sh",
        ):
            with self.subTest(script=script.name):
                self.assertEqual(self._run("bash", "-n", str(script)).returncode, 0)

    def test_release_refuses_a_version_the_metadata_does_not_carry(self) -> None:
        result = self._run(str(SCRIPTS / "release.sh"), "9.9.9")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("versions disagree", result.stderr)
        self.assertFalse((ROOT / "dist" / "v9.9.9").exists())

    def test_a_release_is_never_both_published_and_a_dry_run(self) -> None:
        result = self._run(str(SCRIPTS / "release.sh"), "9.9.9", "--publish", "--allow-dirty")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("don't go together", result.stderr)

    def test_release_keeps_the_signing_key_out_of_its_children(self) -> None:
        script = (SCRIPTS / "release.sh").read_text(encoding="utf-8")

        # Taken into an unexported variable and dropped from the environment before the build, swiftc or
        # notarytool can inherit it; it reaches sign_update on stdin, never argv.
        self.assertIn('ED_KEY="${SPARKLE_ED_PRIVATE_ONYX:-}"', script)
        self.assertLess(script.index("unset SPARKLE_ED_PRIVATE_ONYX"), script.index("launcher/build-app.sh"))
        self.assertIn('printf \'%s\' "$ED_KEY" | "$SIGN_UPDATE" --ed-key-file -', script)
        self.assertNotIn("export ED_KEY", script)
        self.assertNotIn("-s \"$ED_KEY\"", script)

    def test_release_publishes_only_behind_the_flag(self) -> None:
        script = (SCRIPTS / "release.sh").read_text(encoding="utf-8")
        runs = [line for line in script.splitlines() if re.match(r'\s*"\$\{RELEASE_CMD\[@\]\}"\s*$', line)]

        self.assertEqual(len(runs), 1, "the release command runs in exactly one place")
        gate = script[: script.index(runs[0])].rsplit("\n", 3)[-3:]
        self.assertTrue(any('"$PUBLISH" = 1' in line for line in gate), gate)

    def _feed_problem(self, *, url: str, length: int, files: dict[str, bytes] | None = None) -> str:
        if not shutil.which("swiftc"):
            self.skipTest("swiftc builds the signature checker")
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            for name, data in (files or {}).items():
                (folder / name).write_bytes(data)
            feed = folder / "appcast.xml"
            feed.write_text(APPCAST.format(url=url, length=length, signature="AAAA"), encoding="utf-8")
            result = self._run(str(SCRIPTS / "verify-update-feed.sh"), str(feed))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def test_the_feed_check_refuses_an_enclosure_that_is_not_https(self) -> None:
        problem = self._feed_problem(url="http://github.com/cxrobx/onyx/releases/download/v9.9.9/Onyx.zip", length=3)

        self.assertIn("not https", problem)

    def test_the_feed_check_refuses_an_enclosure_it_cannot_find(self) -> None:
        problem = self._feed_problem(url="https://github.com/cxrobx/onyx/releases/download/v9.9.9/Onyx.zip", length=3)

        self.assertIn("Onyx.zip is not in", problem)

    def test_the_feed_check_refuses_a_length_the_file_does_not_have(self) -> None:
        problem = self._feed_problem(
            url="https://github.com/cxrobx/onyx/releases/download/v9.9.9/Onyx.zip",
            length=999,
            files={"Onyx.zip": b"abc"},
        )

        self.assertIn("length mismatch", problem)


if __name__ == "__main__":
    unittest.main()
