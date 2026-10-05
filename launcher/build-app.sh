#!/bin/bash
# Build a self-contained "Onyx.app" and install it to /Applications.
#   ./launcher/build-app.sh          # build + install
#   ./launcher/build-app.sh --no-install
#   ./launcher/build-app.sh --with-mirror     # also bundle the phone mirror's dependencies (or ONYX_WITH_MIRROR=1)
#
# Release signing is opt-in:
#   ONYX_SIGN_IDENTITY="Developer ID Application: …" ./launcher/build-app.sh
#   ONYX_NOTARY_PROFILE="notary-profile" ONYX_SIGN_IDENTITY="…" ./launcher/build-app.sh
# Otherwise a local build signs with the keychain's Apple Development identity,
# or ad hoc when there is none (as in CI). ONYX_SIGN_IDENTITY=- forces ad hoc.
#
# The app updates itself with Sparkle, which this embeds at Contents/Frameworks. The pinned release is
# fetched (and its sha256 checked) by launcher/fetch-sparkle.sh. scripts/release.sh wraps this script to
# make the signed, notarized archive and the appcast that Sparkle reads.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
APP_NAME="Onyx"
BIN_NAME="Onyx"
BUILD="$DIR/build"
BUNDLE="$BUILD/$APP_NAME.app"
BUILDER_VENV="$DIR/.build-venv"
SERVER_DIST="$BUILD/server-dist"
BUILD_LOCK="$ROOT/requirements-build.lock"
SIGN_IDENTITY="${ONYX_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${ONYX_NOTARY_PROFILE:-}"
APP_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$DIR/Info.plist")"
BUILD_ARCH="$(uname -m)"

# The phone mirror is opt-in: its cryptography and QR dependencies go into the bundle only on request,
# so a default build is the build it was (docs/plans/phone-mirror.md, gate 2).
WITH_MIRROR="${ONYX_WITH_MIRROR:-0}"
NO_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-install) NO_INSTALL=1 ;;
    --with-mirror) WITH_MIRROR=1 ;;
  esac
done
MIRROR_BUNDLE_ARGS=()

echo "→ Fetching Sparkle…"
SPARKLE_DIR="$("$DIR/fetch-sparkle.sh")"

if [ ! -x "$BUILDER_VENV/bin/python" ]; then
  echo "→ Creating isolated bundler environment…"
  python3 -m venv "$BUILDER_VENV"
fi

echo "→ Preparing Python service bundler…"
"$BUILDER_VENV/bin/python" -m pip install --disable-pip-version-check \
  --requirement "$BUILD_LOCK"
PYTHON_APP_VERSION="$(PYTHONPATH="$ROOT/src" "$BUILDER_VENV/bin/python" -c \
  'from onyx import __version__; print(__version__)')"
if [ "$PYTHON_APP_VERSION" != "$APP_VERSION" ]; then
  echo "Version mismatch: Python=$PYTHON_APP_VERSION, Info.plist=$APP_VERSION" >&2
  exit 1
fi

if [ "$WITH_MIRROR" = 1 ]; then
  echo "→ Adding the phone mirror's dependencies to the bundler environment…"
  "$BUILDER_VENV/bin/python" -m pip install --disable-pip-version-check \
    "cryptography>=42" "segno>=1.6"
  # onyx.mirror is imported only inside functions and behind mirror.toml, and cryptography ships a compiled
  # backend, so name them all rather than rely on what the import scan happens to find.
  MIRROR_BUNDLE_ARGS=(--collect-submodules onyx.mirror --collect-all cryptography --collect-all segno)
fi

echo "→ Cleaning…"
rm -rf "$BUILD"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources" "$BUNDLE/Contents/Frameworks"

echo "→ Building the Obsidian plugin the app installs…"
PLUGIN="$ROOT/integrations/obsidian"
(cd "$PLUGIN" && npm ci --silent && npm run build --silent)

echo "→ Bundling self-contained local service…"
"$BUILDER_VENV/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --onedir \
  --name onyx-server \
  --paths "$ROOT/src" \
  --add-data "$ROOT/static:static" \
  --add-data "$PLUGIN/manifest.json:obsidian-plugin" \
  --add-data "$PLUGIN/main.js:obsidian-plugin" \
  --add-data "$PLUGIN/styles.css:obsidian-plugin" \
  --collect-all markdown_it \
  --collect-all uvicorn \
  --collect-all pypdf \
  ${MIRROR_BUNDLE_ARGS[@]+"${MIRROR_BUNDLE_ARGS[@]}"} \
  --distpath "$SERVER_DIST" \
  --workpath "$BUILD/pyinstaller-work" \
  --specpath "$BUILD" \
  "$DIR/server-entry.py"
mkdir -p "$BUNDLE/Contents/Resources/Server"
cp -R "$SERVER_DIST/onyx-server/." "$BUNDLE/Contents/Resources/Server/"

echo "→ Drawing icon…"
swiftc -O "$DIR/make-icon.swift" -o "$BUILD/make-icon"
"$BUILD/make-icon" "$BUILD/icon.png" "$DIR/onyx-gem.png"
ICONSET="$BUILD/AppIcon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s        "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}.png"     >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/AppIcon.icns"

echo "→ Packaging Alfred workflow…"
# The name carries no version, so releases/latest/download/Open-in-Onyx.alfredworkflow is a
# permanent link to the newest copy. zip adds to an existing archive, so start from nothing.
ALFRED_WORKFLOW="$BUILD/Open-in-Onyx.alfredworkflow"
rm -f "$ALFRED_WORKFLOW"
/usr/bin/zip -j -q "$ALFRED_WORKFLOW" \
  "$ROOT/integrations/alfred/info.plist" "$ROOT/integrations/alfred/onyx_search.py" "$BUILD/icon.png"

echo "→ Compiling Swift…"
swiftc -F "$SPARKLE_DIR" -framework Cocoa -framework WebKit -framework UniformTypeIdentifiers -framework Sparkle \
  -Xlinker -rpath -Xlinker @executable_path/../Frameworks -O \
  "$DIR/Onyx.swift" -o "$BUNDLE/Contents/MacOS/$BIN_NAME"
# ditto keeps the framework's Versions/Current symlinks, which a plain file copy flattens and breaks.
ditto "$SPARKLE_DIR/Sparkle.framework" "$BUNDLE/Contents/Frameworks/Sparkle.framework"

echo "→ Assembling bundle…"
cp "$DIR/Info.plist" "$BUNDLE/Contents/Info.plist"
# The shipped icon: the gem on a dark square, Liquid Glass on 26+ and flat before it, compiled
# on macOS 26 by .github/workflows/icon.yml (launcher/icon/compile.sh). It is the fallback: at
# launch the app gives itself the transparent gem as a custom icon (adoptCustomIcon in Onyx.swift).
cp "$DIR/icon/Assets.car" "$BUNDLE/Contents/Resources/Assets.car"

echo "→ Signing…"
if [ -n "$SIGN_IDENTITY" ] && [ "$SIGN_IDENTITY" != "-" ]; then
  # Sparkle's helpers, innermost first, as its documentation gives them for Developer ID (no --deep): the
  # two XPC services, the Autoupdate tool, the Updater app, then the framework around them. Each needs the
  # hardened runtime and a timestamp to notarize. The downloader keeps the entitlements it was built with.
  SPARKLE_FW="$BUNDLE/Contents/Frameworks/Sparkle.framework/Versions/B"
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$SPARKLE_FW/XPCServices/Installer.xpc"
  codesign --force --options runtime --timestamp --preserve-metadata=entitlements \
    --sign "$SIGN_IDENTITY" "$SPARKLE_FW/XPCServices/Downloader.xpc"
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$SPARKLE_FW/Autoupdate"
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$SPARKLE_FW/Updater.app"
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$BUNDLE/Contents/Frameworks/Sparkle.framework"
  # --deep never reaches Mach-O files under Contents/Resources, so the frozen service
  # went to the notary unsigned and was rejected. Sign each of its binaries first,
  # innermost first, the executable last, then seal the bundle around them.
  find "$BUNDLE/Contents/Resources/Server" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) \
    -print0 | while IFS= read -r -d '' f; do
      if file -b "$f" | grep -q 'Mach-O'; then echo "$f"; fi
    done | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2- \
    | while IFS= read -r f; do
      codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$f"
    done
  codesign --force --options runtime --timestamp \
    --sign "$SIGN_IDENTITY" "$BUNDLE"
else
  # macOS keeps a permission (Documents, Google Drive) against the app's designated
  # requirement. Signed ad hoc that is the build's cdhash, so every rebuild voided
  # the grants and the service's first vault walk sat in open() on a consent prompt
  # for minutes. A certificate makes it identifier + certificate, which a rebuild keeps.
  DEV_IDENTITY=""
  if [ -z "$SIGN_IDENTITY" ]; then
    DEV_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
      | awk '/"Apple Development: /{print $2; exit}' || true)"
  fi
  if [ -n "$DEV_IDENTITY" ] && codesign --force --deep --sign "$DEV_IDENTITY" "$BUNDLE"; then
    echo "  Signed with the Apple Development identity $DEV_IDENTITY"
  else
    if [ -n "$DEV_IDENTITY" ]; then
      echo "  Couldn't sign with $DEV_IDENTITY; signing ad hoc, so macOS will ask for its permissions again." >&2
    fi
    codesign --force --deep --sign - "$BUNDLE"
  fi
fi
codesign --verify --deep --strict --verbose=2 "$BUNDLE"

ARCHIVE="$BUILD/Onyx-$APP_VERSION-macOS-$BUILD_ARCH.zip"
DMG="$BUILD/Onyx-$APP_VERSION-macOS-$BUILD_ARCH.dmg"
echo "→ Creating release archive…"
ditto -c -k --sequesterRsrc --keepParent "$BUNDLE" "$ARCHIVE"

if [ -n "$NOTARY_PROFILE" ]; then
  if [ -z "$SIGN_IDENTITY" ] || [ "$SIGN_IDENTITY" = "-" ]; then
    echo "ONYX_NOTARY_PROFILE requires ONYX_SIGN_IDENTITY." >&2
    exit 1
  fi
  echo "→ Submitting release archive for notarization…"
  xcrun notarytool submit "$ARCHIVE" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$BUNDLE"
  xcrun stapler validate "$BUNDLE"
  rm -f "$ARCHIVE"
  ditto -c -k --sequesterRsrc --keepParent "$BUNDLE" "$ARCHIVE"
fi

echo "→ Creating drag-to-Applications disk image…"
DMG_CONTENTS="$BUILD/dmg-contents"
mkdir -p "$DMG_CONTENTS"
ditto "$BUNDLE" "$DMG_CONTENTS/$APP_NAME.app"
ln -s /Applications "$DMG_CONTENTS/Applications"
hdiutil create -volname "$APP_NAME $APP_VERSION" -srcfolder "$DMG_CONTENTS" \
  -format UDZO -ov "$DMG"
if [ -n "$SIGN_IDENTITY" ] && [ "$SIGN_IDENTITY" != "-" ]; then
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
fi

if [ -n "$NOTARY_PROFILE" ]; then
  echo "→ Submitting disk image for notarization…"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
fi
(
  cd "$BUILD"
  LC_ALL=C shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256"
  LC_ALL=C shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256"
  LC_ALL=C shasum -a 256 "$(basename "$ALFRED_WORKFLOW")" > "$(basename "$ALFRED_WORKFLOW").sha256"
)

if [ "$NO_INSTALL" = 1 ]; then
  echo "✓ Built: $BUNDLE"
  echo "✓ Archive: $ARCHIVE"
  echo "✓ Disk image: $DMG"
  echo "✓ Alfred workflow: $ALFRED_WORKFLOW"
  exit 0
fi

DEST="/Applications/$APP_NAME.app"
STAGED="$BUILD/install/$APP_NAME.app"
BACKUP="$BUILD/install/$APP_NAME.previous.app"
echo "→ Installing to ${DEST}"
mkdir -p "$BUILD/install"
rm -rf "$STAGED" "$BACKUP"
cp -R "$BUNDLE" "$STAGED"
if [ -e "$DEST" ]; then
  mv "$DEST" "$BACKUP"
fi
if ! mv "$STAGED" "$DEST"; then
  if [ -e "$BACKUP" ]; then mv "$BACKUP" "$DEST"; fi
  echo "Install failed; the previous app was restored." >&2
  exit 1
fi
rm -rf "$BACKUP"
# Own it as the user's primary group. A build run from a HEAD export under /private/tmp carries that
# directory's group, wheel, into /Applications, and Sparkle then can't give its staged copy the same
# group, decides it needs an admin password, and fails with "An error occurred while launching the installer".
chown -R "$(id -un):$(id -gn)" "$DEST"
# Clear the quarantine flag so it opens without the unidentified-developer prompt.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
# The transparent gem, as the installed app's custom icon. The app sets it itself at launch
# (adoptCustomIcon in Onyx.swift), but this install just replaced the bundle that carried it, and ⌘W
# hides Onyx rather than quitting it: until the next launch, whatever reads the icon from disk
# (Finder, a Dock replacement, the Dock once Onyx quits) shows the dark square. The archive and disk
# image keep the bundle exactly as signed.
if [ "$(defaults read com.cx.onyx customIcon 2>/dev/null)" = 0 ]; then
  echo "  Custom icon: off (defaults com.cx.onyx customIcon)"
elif [ "$(osascript -l JavaScript -e '
  ObjC.import("AppKit")
  function run(argv) {
    const gem = $.NSImage.alloc.initWithContentsOfFile(argv[0] + "/Contents/Resources/AppIcon.icns")
    return !gem.isNil() && $.NSWorkspace.sharedWorkspace.setIconForFileOptions(gem, argv[0], 0)
  }' "$DEST" 2>/dev/null)" = true ]; then
  echo "  Custom icon: the transparent gem"
else
  echo "  Couldn't set the custom icon; Onyx sets it on its next launch." >&2
fi
/System/Library/CoreServices/pbs -update >/dev/null 2>&1 || true
# The headless service (scripts/install-daemon.sh) keeps running the bundle it started from, and the
# app adopts whatever healthy service owns the port: left alone, the new app shows the old pages, and
# macOS stops honouring the running copy's Documents access once its files are replaced. Restart it
# on this build.
DAEMON="gui/$(id -u)/com.cx.onyx.server"
if launchctl print "$DAEMON" >/dev/null 2>&1; then
  echo "→ Restarting the background service on this build"
  launchctl kickstart -k "$DAEMON" >/dev/null 2>&1 \
    || echo "  Couldn't restart it; run: launchctl kickstart -k $DAEMON" >&2
fi

echo ""
echo "✓ Installed: $DEST"
if pgrep -xq "$APP_NAME"; then
  echo "  $APP_NAME is open: quit and reopen it to run this build."
fi
echo "  Archive: $ARCHIVE"
echo "  Disk image: $DMG"
echo "  Alfred workflow: $ALFRED_WORKFLOW"
echo "  Open it from Spotlight/Launchpad as 'Onyx', or:  open -a 'Onyx'"
