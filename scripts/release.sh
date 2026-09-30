#!/bin/bash
# Make a release: build, sign and notarize the app, sign its update archive for Sparkle, write the appcast,
# check the feed the way an installed copy would, and gather it all in dist/vVERSION.
#
#   ONYX_NOTARY_PROFILE=<notarytool keychain profile> scripts/release.sh 0.6.4
#
# It stops short of publishing. It prints the `gh release create` command, and runs it only when given
# --publish, so the release is one you read first. Nothing is uploaded and no tag is made otherwise.
#
#   --publish      also run the printed gh release create (a clean tree, pushed commit)
#   --allow-dirty  a dry run from a tree with uncommitted work; the result is never publishable
#   --check        run every check that comes before the build (versions, tag, identity, notary profile,
#                  signing key) and stop
#
# Needs: a Developer ID Application identity in the keychain (ONYX_SIGN_IDENTITY, or the only one there
# is), a notarytool profile (ONYX_NOTARY_PROFILE), and the Sparkle EdDSA key that matches SUPublicEDKey in
# launcher/Info.plist. Prefer handing it over as SPARKLE_ED_PRIVATE_ONYX:
#   ONYX_NOTARY_PROFILE=… secret run -k SPARKLE_ED_PRIVATE_ONYX -- scripts/release.sh 0.6.4
# It is read into a shell variable and removed from the environment before anything else runs, so the build,
# swiftc and notarytool never see it, and it reaches sign_update on stdin, never argv. Without that variable
# it uses the login Keychain's Sparkle account "onyx" (`generate_keys --account onyx`), where macOS may stop
# to ask for the login password the first time a tool reads the key. Either way the key is checked against
# SUPublicEDKey before the build starts. Never put the private key in this repo.
set -euo pipefail

# The key, taken out of the environment at once. A shell variable that is not exported is not inherited.
ED_KEY="${SPARKLE_ED_PRIVATE_ONYX:-}"
unset SPARKLE_ED_PRIVATE_ONYX

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
REPO="cxrobx/onyx"
PLIST="$ROOT/launcher/Info.plist"
PLISTBUDDY=/usr/libexec/PlistBuddy

fail() { echo "✗ $*" >&2; exit 1; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" >&2; exit 2; }

VERSION=""
PUBLISH=0
ALLOW_DIRTY=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) usage ;;
    -*) echo "Unknown option $arg" >&2; usage ;;
    *) [ -z "$VERSION" ] || usage; VERSION="$arg" ;;
  esac
done
[ -n "$VERSION" ] || usage
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "VERSION must look like 0.6.4, not '$VERSION'"
[ "$PUBLISH" = 0 ] || [ "$ALLOW_DIRTY" = 0 ] || fail "--publish and --allow-dirty don't go together: a dry run is never published"

TAG="v$VERSION"
NOTES="release-notes/$TAG.md"

echo "→ Checking $TAG is releasable…"
# The four places a version lives must be one: the test suite holds them to it, and so does this.
read_versions() {
  python3 - "$ROOT" <<'PY'
import re, sys, tomllib
from pathlib import Path
root = Path(sys.argv[1])
print(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])
print(re.search(r'__version__\s*=\s*"([^"]+)"', (root / "src/onyx/__init__.py").read_text()).group(1))
PY
  $PLISTBUDDY -c 'Print :CFBundleShortVersionString' "$PLIST"
  $PLISTBUDDY -c 'Print :CFBundleVersion' "$PLIST"
}
VERSIONS="$(read_versions)"
if [ "$(echo "$VERSIONS" | sort -u | wc -l | tr -d ' ')" != 1 ] || [ "$(echo "$VERSIONS" | head -1)" != "$VERSION" ]; then
  echo "$VERSIONS" | paste -sd' ' - | sed 's/^/  pyproject, __version__, CFBundleShortVersionString, CFBundleVersion: /' >&2
  fail "the versions disagree with each other or with $VERSION"
fi
[ -f "$NOTES" ] || fail "$NOTES doesn't exist: write the release notes first"
if [ -n "$(git status --porcelain)" ]; then
  if [ "$ALLOW_DIRTY" = 1 ]; then
    echo "  ! The tree has uncommitted changes (--allow-dirty): this is a dry run, not a release." >&2
  else
    git status --short >&2
    fail "the git tree is dirty. Commit or stash first (or --allow-dirty for a dry run)."
  fi
fi
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  fail "the tag $TAG already exists"
fi
if command -v gh >/dev/null 2>&1 && gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  fail "a GitHub release for $TAG already exists"
fi

FEED_URL="$($PLISTBUDDY -c 'Print :SUFeedURL' "$PLIST" 2>/dev/null || true)"
PUBLIC_KEY="$($PLISTBUDDY -c 'Print :SUPublicEDKey' "$PLIST" 2>/dev/null || true)"
[[ "$FEED_URL" == https://github.com/$REPO/releases/latest/download/appcast.xml ]] || fail "SUFeedURL in Info.plist is '$FEED_URL'"
[ -n "$PUBLIC_KEY" ] || fail "SUPublicEDKey is missing from Info.plist"

IDENTITY="${ONYX_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  FOUND="$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2}')"
  [ "$(echo "$FOUND" | grep -c .)" = 1 ] || fail "set ONYX_SIGN_IDENTITY: the keychain has $(echo "$FOUND" | grep -c .) Developer ID Application identities, not one"
  IDENTITY="$FOUND"
fi
case "$IDENTITY" in "Developer ID Application:"*) ;; *) fail "'$IDENTITY' is not a Developer ID Application identity" ;; esac
[ -n "${ONYX_NOTARY_PROFILE:-}" ] || fail "set ONYX_NOTARY_PROFILE to a notarytool keychain profile"
xcrun notarytool history --keychain-profile "$ONYX_NOTARY_PROFILE" >/dev/null 2>&1 \
  || fail "notarytool can't use the keychain profile '$ONYX_NOTARY_PROFILE'"

SPARKLE_DIR="$("$ROOT/launcher/fetch-sparkle.sh")"
SIGN_UPDATE="$SPARKLE_DIR/bin/sign_update"

# Sparkle's EdDSA signature of a file, on stdout.
sign_file() {
  if [ -n "$ED_KEY" ]; then
    printf '%s' "$ED_KEY" | "$SIGN_UPDATE" --ed-key-file - -p "$1"
  else
    "$SIGN_UPDATE" --account onyx -p "$1"
  fi
}

# Before a ten-minute build: sign a probe and check it against the public key the app carries, so a missing,
# wrong or unreadable key (or a password prompt nobody is there to answer) fails now, not at the end.
TOOLS="$(mktemp -d "${TMPDIR:-/tmp}/onyx-release.XXXXXX")"
trap 'rm -rf "$TOOLS"' EXIT
swiftc -O "$ROOT/scripts/ed25519-verify.swift" -o "$TOOLS/ed25519-verify" 2>/dev/null \
  || fail "couldn't build scripts/ed25519-verify.swift (needs the Xcode command-line tools)"
echo "onyx release key check" > "$TOOLS/probe"
PROBE_SIGNATURE="$(sign_file "$TOOLS/probe" 2>/dev/null)" \
  || fail "couldn't sign with the Sparkle key. Run under: secret run -k SPARKLE_ED_PRIVATE_ONYX -- $0 $VERSION"
"$TOOLS/ed25519-verify" "$PUBLIC_KEY" "$TOOLS/probe" "$PROBE_SIGNATURE" \
  || fail "the Sparkle signing key does not match SUPublicEDKey in launcher/Info.plist"
echo "  ✓ versions agree on $VERSION, $TAG is free, identity and notary profile work, the signing key matches"
if [ "$CHECK_ONLY" = 1 ]; then
  echo "✓ Every check before the build passed (--check): nothing was built."
  exit 0
fi

echo "→ Building, signing and notarizing Onyx $VERSION (this takes a while)…"
ONYX_SIGN_IDENTITY="$IDENTITY" ONYX_NOTARY_PROFILE="$ONYX_NOTARY_PROFILE" "$ROOT/launcher/build-app.sh" --no-install

BUILD="$ROOT/launcher/build"
ARCH="$(uname -m)"
ZIP_NAME="Onyx-$VERSION-macOS-$ARCH.zip"
DIST="$ROOT/dist/$TAG"
rm -rf "$DIST"
mkdir -p "$DIST"
for f in "$ZIP_NAME" "$ZIP_NAME.sha256" "Onyx-$VERSION-macOS-$ARCH.dmg" "Onyx-$VERSION-macOS-$ARCH.dmg.sha256" \
         Open-in-Onyx.alfredworkflow Open-in-Onyx.alfredworkflow.sha256; do
  [ -f "$BUILD/$f" ] || fail "the build did not produce $f"
  cp "$BUILD/$f" "$DIST/$f"
done

echo "→ Signing the update archive for Sparkle…"
SIGNATURE="$(sign_file "$DIST/$ZIP_NAME")"
LENGTH="$(stat -f %z "$DIST/$ZIP_NAME")"
[ -n "$SIGNATURE" ] || fail "sign_update produced no signature"

echo "→ Writing the appcast…"
MIN_OS="$($PLISTBUDDY -c 'Print :LSMinimumSystemVersion' "$PLIST")"
python3 - "$DIST/appcast.xml" "$ROOT/$NOTES" "$VERSION" "$REPO" "$ZIP_NAME" "$LENGTH" "$SIGNATURE" "$MIN_OS" "$ARCH" <<'PY'
import sys
from email.utils import formatdate
from xml.sax.saxutils import escape, quoteattr

out, notes_path, version, repo, zip_name, length, signature, min_os, arch = sys.argv[1:]
notes = open(notes_path, encoding="utf-8").read().strip()
if "]]>" in notes:
    sys.exit("the release notes contain ']]>', which cannot sit in a CDATA section")
min_os = min_os if min_os.count(".") >= 2 else min_os + ".0"  # Sparkle wants three parts: 13.0.0
url = f"https://github.com/{repo}/releases/download/v{version}/{zip_name}"
# The frozen service is built for the machine that builds it, so an arm64 release must not be offered to an Intel Mac.
requirements = "            <sparkle:hardwareRequirements>arm64</sparkle:hardwareRequirements>\n" if arch == "arm64" else ""
xml = f'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
    <channel>
        <title>Onyx</title>
        <link>https://github.com/{repo}/releases</link>
        <description>Onyx updates</description>
        <language>en</language>
        <item>
            <title>Onyx {escape(version)}</title>
            <link>https://github.com/{repo}/releases/tag/v{escape(version)}</link>
            <sparkle:version>{escape(version)}</sparkle:version>
            <sparkle:shortVersionString>{escape(version)}</sparkle:shortVersionString>
            <pubDate>{formatdate(usegmt=True)}</pubDate>
            <sparkle:minimumSystemVersion>{escape(min_os)}</sparkle:minimumSystemVersion>
{requirements}            <description sparkle:format="markdown"><![CDATA[
{notes}
]]></description>
            <enclosure url={quoteattr(url)} length={quoteattr(length)} type="application/octet-stream" sparkle:edSignature={quoteattr(signature)} />
        </item>
    </channel>
</rss>
'''
open(out, "w", encoding="utf-8").write(xml)
PY

echo "→ Verifying the feed as an installed copy would…"
"$ROOT/scripts/verify-update-feed.sh" "$DIST/appcast.xml" --expect-version "$VERSION"

cd "$ROOT"
FILES=("$DIST/Onyx-$VERSION-macOS-$ARCH.dmg" "$DIST/Onyx-$VERSION-macOS-$ARCH.dmg.sha256" "$DIST/$ZIP_NAME" "$DIST/$ZIP_NAME.sha256" \
       "$DIST/Open-in-Onyx.alfredworkflow" "$DIST/Open-in-Onyx.alfredworkflow.sha256" "$DIST/appcast.xml")
RELEASE_CMD=(gh release create "$TAG" --repo "$REPO" --target "$(git rev-parse HEAD)" --title "Onyx $VERSION" --notes-file "$ROOT/$NOTES" "${FILES[@]}")

echo ""
echo "✓ Release $VERSION is ready in $DIST"
ls -l "$DIST" | sed 's/^/  /'
echo ""
if [ "$ALLOW_DIRTY" = 1 ]; then
  echo "This was a dry run from a dirty tree (--allow-dirty): do not publish it. Commit, then run this again."
  exit 0
fi
echo "To publish, push the release commit, then run:"
printf '  '; printf '%q ' "${RELEASE_CMD[@]}"; echo ""
if [ "$PUBLISH" = 1 ]; then
  echo "→ --publish given: running it."
  "${RELEASE_CMD[@]}"
else
  echo "(Not run. Re-run with --publish to have this script run it.)"
fi
