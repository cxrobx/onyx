#!/bin/bash
# Check that a Sparkle appcast would actually update an installed Onyx: fetch the feed, download every
# enclosure, and fail on anything the updater would refuse or that would strand the apps already installed.
#
#   scripts/verify-update-feed.sh                      # the production feed (launcher/Info.plist's SUFeedURL)
#   scripts/verify-update-feed.sh URL                  # another feed, https only
#   scripts/verify-update-feed.sh dist/v0.6.4/appcast.xml   # a local dry run: each enclosure is read from the
#                                                      # feed's folder, by the name its https URL ends in
#   options: --expect-version V    the newest item must be V
#            --enclosure-dir DIR   read a local feed's enclosures from DIR instead of its folder
#            --test-build          for a scratch build: skip the notarization check, and allow an http feed
#                                  and enclosures when they are on 127.0.0.1 or localhost
#
# For each item it checks: the enclosure URL is https; the file is as long as the feed says; its EdDSA
# signature verifies against the public key in launcher/Info.plist (the key every installed copy trusts,
# which `sign_update --verify` cannot check, as it wants the private half); and the app inside carries the
# version the feed advertises, the same public key, an https feed of its own, a valid code signature and a
# stapled notarization ticket. An update failing any of these is one the next release could not be reached from.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$ROOT/launcher/Info.plist"
PLISTBUDDY=/usr/libexec/PlistBuddy

FEED=""
EXPECT_VERSION=""
ENCLOSURE_DIR=""
TEST_BUILD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-version) EXPECT_VERSION="${2:?--expect-version needs a value}"; shift 2 ;;
    --enclosure-dir) ENCLOSURE_DIR="${2:?--enclosure-dir needs a value}"; shift 2 ;;
    --test-build) TEST_BUILD=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "Unknown option $1" >&2; exit 2 ;;
    *) FEED="$1"; shift ;;
  esac
done

fail() { echo "✗ $*" >&2; exit 1; }
ok() { echo "  ✓ $*"; }

PUBLIC_KEY="$($PLISTBUDDY -c 'Print :SUPublicEDKey' "$PLIST" 2>/dev/null || true)"
[ -n "$PUBLIC_KEY" ] || fail "launcher/Info.plist has no SUPublicEDKey"
[ -n "$FEED" ] || FEED="$($PLISTBUDDY -c 'Print :SUFeedURL' "$PLIST")"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/onyx-feed.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# https all the way, redirects included: GitHub hands a release asset to another host. A test build may use
# http, but only to 127.0.0.1 or localhost.
fetch() {
  local only_https=(--proto '=https' --proto-redir '=https')
  if [ "$TEST_BUILD" = 1 ]; then
    case "$1" in http://127.0.0.1[:/]*|http://localhost[:/]*) only_https=() ;; esac
  fi
  curl --fail --silent --show-error --location ${only_https[@]+"${only_https[@]}"} --retry 2 --output "$2" "$1"
}

REMOTE=0
case "$FEED" in
  https://*) REMOTE=1; echo "→ Fetching $FEED"; fetch "$FEED" "$WORK/appcast.xml" || fail "couldn't fetch the feed"; FEED_FILE="$WORK/appcast.xml" ;;
  http://*) if [ "$TEST_BUILD" = 1 ]; then
              case "$FEED" in http://127.0.0.1[:/]*|http://localhost[:/]*) ;; *) fail "even a test build's http feed must be on localhost: $FEED" ;; esac
              REMOTE=1; echo "→ Fetching $FEED (test build: http on localhost)"
              fetch "$FEED" "$WORK/appcast.xml" || fail "couldn't fetch the feed"
              FEED_FILE="$WORK/appcast.xml"
            else fail "the feed must be https: $FEED"; fi ;;
  *) [ -f "$FEED" ] || fail "no such feed file: $FEED"
     FEED_FILE="$FEED"; echo "→ Reading $FEED"
     [ -n "$ENCLOSURE_DIR" ] || ENCLOSURE_DIR="$(cd "$(dirname "$FEED")" && pwd)" ;;
esac

# One tab-separated line per item: version, short version, url, length, signature.
python3 - "$FEED_FILE" > "$WORK/items.tsv" <<'PY' || fail "the feed is not a readable appcast"
import sys
import xml.etree.ElementTree as ET

SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
items = ET.parse(sys.argv[1]).getroot().findall("./channel/item")
if not items:
    sys.exit("the feed has no items")
for item in items:
    enclosure = item.find("enclosure")
    if enclosure is None:
        sys.exit("an item has no enclosure")
    version = (item.findtext(SPARKLE + "version") or enclosure.get(SPARKLE + "version") or "").strip()
    short = (item.findtext(SPARKLE + "shortVersionString") or enclosure.get(SPARKLE + "shortVersionString") or "").strip()
    fields = [version, short or "-", enclosure.get("url", ""), enclosure.get("length", ""), enclosure.get(SPARKLE + "edSignature", "")]
    if not (version and fields[2] and fields[3] and fields[4]):
        sys.exit(f"an item is missing its version, url, length or edSignature: {fields}")
    print("\t".join(fields))
PY

COUNT="$(wc -l < "$WORK/items.tsv" | tr -d ' ')"
echo "→ $COUNT item(s)"

VERIFIER="$WORK/ed25519-verify"
swiftc -O "$ROOT/scripts/ed25519-verify.swift" -o "$VERIFIER" 2>/dev/null \
  || fail "couldn't build scripts/ed25519-verify.swift (needs the Xcode command-line tools)"

NEWEST=""
# The feed is read on fd 3 so nothing run in the loop can eat it.
while IFS=$'\t' read -r -u 3 VERSION SHORT URL LENGTH SIGNATURE; do
  [ "$SHORT" != "-" ] || SHORT=""
  echo "→ Onyx $VERSION ($URL)"
  case "$URL" in
    https://*) ok "enclosure is https" ;;
    http://127.0.0.1[:/]*|http://localhost[:/]*)
      [ "$TEST_BUILD" = 1 ] && echo "  - enclosure is http on localhost (--test-build)" \
        || fail "enclosure is not https: $URL" ;;
    *) fail "enclosure is not https: $URL" ;;
  esac
  [[ "$LENGTH" =~ ^[0-9]+$ ]] || fail "length is not a number: $LENGTH"

  NAME="$(python3 -c 'import sys, urllib.parse as u; print(u.unquote(u.urlparse(sys.argv[1]).path.rsplit("/", 1)[-1]))' "$URL")"
  FILE="$WORK/$VERSION-$NAME"
  if [ "$REMOTE" = 1 ] && [ -z "$ENCLOSURE_DIR" ]; then
    fetch "$URL" "$FILE" || fail "couldn't download $URL"
  else
    LOCAL="$ENCLOSURE_DIR/$NAME"
    [ -f "$LOCAL" ] || fail "$NAME is not in $ENCLOSURE_DIR (the feed's https URL isn't fetched for a local feed)"
    FILE="$LOCAL"
  fi
  ACTUAL="$(stat -f %z "$FILE")"
  [ "$ACTUAL" = "$LENGTH" ] || fail "length mismatch: the feed says $LENGTH bytes, the file is $ACTUAL"
  ok "length $LENGTH"

  "$VERIFIER" "$PUBLIC_KEY" "$FILE" "$SIGNATURE" \
    || fail "the EdDSA signature does not verify against SUPublicEDKey in launcher/Info.plist"
  ok "EdDSA signature verifies against the app's public key"

  mkdir "$WORK/app-$VERSION"
  ditto -x -k "$FILE" "$WORK/app-$VERSION" || fail "couldn't unzip the enclosure"
  APP="$(find "$WORK/app-$VERSION" -maxdepth 1 -name '*.app' -print -quit)"
  [ -n "$APP" ] || fail "no .app at the top of the archive"
  INFO="$APP/Contents/Info.plist"
  [ "$($PLISTBUDDY -c 'Print :CFBundleVersion' "$INFO")" = "$VERSION" ] \
    || fail "the feed says sparkle:version $VERSION; the app's CFBundleVersion is $($PLISTBUDDY -c 'Print :CFBundleVersion' "$INFO")"
  if [ -n "$SHORT" ]; then
    [ "$($PLISTBUDDY -c 'Print :CFBundleShortVersionString' "$INFO")" = "$SHORT" ] || fail "shortVersionString does not match the app"
  fi
  [ "$($PLISTBUDDY -c 'Print :SUPublicEDKey' "$INFO" 2>/dev/null)" = "$PUBLIC_KEY" ] \
    || fail "the app inside carries a different SUPublicEDKey than launcher/Info.plist: installed copies could not update from it"
  APP_FEED="$($PLISTBUDDY -c 'Print :SUFeedURL' "$INFO" 2>/dev/null || true)"
  case "$APP_FEED" in
    https://*) ;;
    *) [ "$TEST_BUILD" = 1 ] || fail "the app's own SUFeedURL is not https: '$APP_FEED'" ;;
  esac
  ok "app $($PLISTBUDDY -c 'Print :CFBundleShortVersionString' "$INFO") ($VERSION): same public key, feed $APP_FEED"

  codesign --verify --deep --strict "$APP" 2>"$WORK/codesign.err" || fail "the app's code signature is invalid: $(cat "$WORK/codesign.err")"
  ok "code signature valid (--deep --strict)"
  if [ "$TEST_BUILD" = 1 ]; then
    echo "  - notarization not checked (--test-build)"
  else
    xcrun stapler validate "$APP" >/dev/null 2>&1 || fail "the app has no stapled notarization ticket"
    ok "notarization ticket stapled"
  fi

  if [ -z "$NEWEST" ] || python3 -c 'import sys
def key(v): return [int(p) if p.isdigit() else 0 for p in v.split(".")]
sys.exit(0 if key(sys.argv[1]) > key(sys.argv[2]) else 1)' "$VERSION" "$NEWEST"; then
    NEWEST="$VERSION"
  fi
done 3< "$WORK/items.tsv"

if [ -n "$EXPECT_VERSION" ] && [ "$NEWEST" != "$EXPECT_VERSION" ]; then
  fail "the newest item is $NEWEST, expected $EXPECT_VERSION"
fi
echo "✓ Feed OK: $COUNT item(s), newest $NEWEST"
