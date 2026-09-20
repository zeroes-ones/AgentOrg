#!/usr/bin/env bash
#
# run-macos-app.sh — build AgentOrg into a real .app and launch it.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# `swift run AgentOrg` does start the app, but SwiftPM produces a *bare executable*. macOS decides how
# to treat a process from its bundle: without an `Info.plist` the app has no Dock presence, no proper
# menu bar, and — most visibly — the window does not reliably come to the front, so it looks like
# nothing happened. Assembling a minimal `.app` around the same binary is what makes it behave like
# an application rather than a background process.
#
# It also keeps the launch reproducible: one command builds, bundles and opens, so "how do I run it?"
# has one answer instead of a sequence someone has to reconstruct.
#
# It also renders the app icon from `macos/Resources/AppIcon.svg` into the bundle's Resources. That is
# here rather than in a separate script because the icon *is* part of assembling the bundle — it is the
# one thing macOS draws about the app before a person has run it — and because a second script is one
# more thing for a reader to discover before understanding what "build the app" means.
#
# Usage:
#   scripts/run-macos-app.sh              # build (release), bundle, launch
#   scripts/run-macos-app.sh --debug      # a debug build, for a stack trace worth reading
#   scripts/run-macos-app.sh --build-only # assemble the .app without launching
#
# Requires `rsvg-convert` (brew install librsvg) to render the icon. Its absence is reported and the
# build continues without one rather than failing.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MACOS_DIR="$REPO_ROOT/macos"
APP_NAME="AgentOrg"
BUNDLE="$MACOS_DIR/.build/$APP_NAME.app"
CONFIG="release"

for arg in "$@"; do
  case "$arg" in
    --debug) CONFIG="debug" ;;
    --build-only) LAUNCH=0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
LAUNCH="${LAUNCH:-1}"

# ── 1. build ────────────────────────────────────────────────────────────────
echo "==> building ($CONFIG)"
swift build --package-path "$MACOS_DIR" -c "$CONFIG" --disable-sandbox

BINARY="$MACOS_DIR/.build/$CONFIG/$APP_NAME"
if [[ ! -x "$BINARY" ]]; then
  echo "build produced no executable at $BINARY" >&2
  exit 1
fi

# ── 2. assemble the bundle ──────────────────────────────────────────────────
# A minimal, valid .app: the binary, and an Info.plist that tells macOS this is a regular
# foreground application rather than an accessory.
echo "==> assembling $BUNDLE"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BINARY" "$BUNDLE/Contents/MacOS/$APP_NAME"
chmod +x "$BUNDLE/Contents/MacOS/$APP_NAME"

# ── the icon ────────────────────────────────────────────────────────────────
# Rendered from macos/Resources/AppIcon.svg rather than committed as a blob, so the icon is a
# reviewable text change and the repo holds no unexplainable binary. Skipped when the .icns is already
# newer than the SVG, because this runs on every launch.
#
# **A missing icon is not fatal to the bundle.** The build still produces a working .app; it just gets
# the generic macOS one. Failing the whole launch because Homebrew's librsvg is absent would be worse
# than a plain Dock tile.
ICON_NAME="AppIcon"
ICON_SVG="$MACOS_DIR/Resources/AppIcon.svg"
ICON_ICNS="$MACOS_DIR/.build/$ICON_NAME.icns"

icon_ready=0
if [[ ! -f "$ICON_ICNS" || "$ICON_SVG" -nt "$ICON_ICNS" ]]; then
  # rsvg-convert is the one non-Apple tool this needs, and it is not on a stock Mac — so its absence is
  # named with the fix rather than left to fail as "command not found" halfway through a bundle.
  if ! command -v rsvg-convert >/dev/null 2>&1; then
    echo "note: rsvg-convert not found, so no icon can be rendered (brew install librsvg)" >&2
  elif [[ ! -r "$ICON_SVG" ]]; then
    echo "note: no icon source at $ICON_SVG" >&2
  else
    # The sizes macOS selects from per context: 16/32 for the menu bar and list rows, 128 for Finder,
    # 256/512 for a large Dock, and the @2x of each rendered *directly* at its pixel size rather than
    # upscaled — on a Retina display the @2x image is the one drawn, so an upscaled one is visibly soft
    # at exactly the size people look at most.
    ICONSET="$(mktemp -d)/$ICON_NAME.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 128 256 512; do
      rsvg-convert -w "$size" -h "$size" -o "$ICONSET/icon_${size}x${size}.png" "$ICON_SVG"
      double=$((size * 2))
      rsvg-convert -w "$double" -h "$double" \
        -o "$ICONSET/icon_${size}x${size}@2x.png" "$ICON_SVG"
    done
    mkdir -p "$(dirname "$ICON_ICNS")"
    if iconutil -c icns "$ICONSET" -o "$ICON_ICNS"; then
      icon_ready=1
      echo "==> rendered the app icon from Resources/AppIcon.svg"
    else
      echo "note: iconutil could not assemble the icon set" >&2
    fi
    rm -rf "$(dirname "$ICONSET")"
  fi
elif [[ -f "$ICON_ICNS" ]]; then
  icon_ready=1
fi

if [[ "$icon_ready" == "1" && -f "$ICON_ICNS" ]]; then
  cp "$ICON_ICNS" "$BUNDLE/Contents/Resources/$ICON_NAME.icns"
else
  echo "note: the bundle keeps the generic macOS icon" >&2
  ICON_NAME=""
fi

# NOTE FOR EDITORS: this heredoc is deliberately **unquoted** so `$APP_NAME` and `$ICON_NAME` expand.
# That also means bash runs anything that looks like a substitution inside it. Backticks and `$` in a
# comment here are executed, not printed — so no comment in this block may contain either. (An earlier
# version of the icon comment below used a backtick-quoted filename and the build tried to run it.)
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>AgentOrg</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>dev.agentorg.console</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <!-- The Dock, Cmd-Tab and menu-bar tile. An empty string when the renderer was unavailable, which
       macOS reads as "no icon" and falls back to the generic one — the same result as omitting the
       key, and simpler than two plists. -->
  <key>CFBundleIconFile</key><string>$ICON_NAME</string>
  <!-- A regular app: it owns a window, a menu bar and a Dock icon. -->
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <!-- Why macOS asks, and what to say. The app locates its engine by walking up from its own binary
       until it finds the engine's cli.py, and that repository very often lives under ~/Documents — a
       TCC-protected folder. Without this key the system prompt is a bare "AgentOrg would like to
       access files in your Documents folder", which reads as the app wanting something it has no
       business wanting, and denying it leaves the console stuck at "launching the engine" forever
       with nothing on screen explaining why. -->
  <key>NSDocumentsFolderUsageDescription</key>
  <string>AgentOrg runs its engine from the AgentOrg project folder. If that folder is inside Documents, macOS asks for access so the console can start the engine and read the run history it writes there.</string>
  <key>NSDesktopFolderUsageDescription</key>
  <string>AgentOrg runs its engine from the AgentOrg project folder. If that folder is inside the Desktop, macOS asks for access so the console can start the engine and read the run history it writes there.</string>
</dict>
</plist>
PLIST

# Ad-hoc signature. Not a Developer ID — enough for the app to launch locally without Gatekeeper
# quarantining it, which is the difference between "it opens" and "it is damaged".
codesign --force --deep --sign - "$BUNDLE" >/dev/null 2>&1 \
  || echo "note: ad-hoc codesign skipped (the app still runs locally)"

echo "==> built $BUNDLE"

# ── 3. launch ───────────────────────────────────────────────────────────────
if [[ "$LAUNCH" == "1" ]]; then
  # Quit any running instance FIRST. `open` on an already-running app just brings it to the front,
  # so a rebuild would silently keep serving the OLD binary — which is indistinguishable from "my
  # fix didn't work" and is exactly the confusion this step exists to remove. `open -n` would spawn
  # a second copy instead, which is worse: two engines on one project.
  if osascript -e 'tell application "System Events" to (name of processes) contains "AgentOrg"' \
       2>/dev/null | grep -qi true; then
    echo "==> quitting the running AgentOrg so the new build is used"
    osascript -e 'tell application "AgentOrg" to quit' 2>/dev/null || true
    # Give it a moment to release the engine subprocess and its project lock.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if ! osascript -e 'tell application "System Events" to (name of processes) contains "AgentOrg"' \
           2>/dev/null | grep -qi true; then break; fi
      sleep 0.3
    done
  fi

  # `open` hands the app to launchd, so it gets a proper GUI session and comes to the front.
  # Running the binary directly works but is what makes the window easy to miss.
  echo "==> launching"
  open "$BUNDLE"
  echo
  echo "AgentOrg is running. Point it at a project with:"
  echo "  AGENTORG_SKILLS_ROOT=/path/to/Skills \\"
  echo "    open -a $BUNDLE --env AGENTORG_SKILLS_ROOT=\$AGENTORG_SKILLS_ROOT"
  echo
  echo "It launches the engine itself (\`engine.cli serve\`), so the console needs no separate server."
  echo "Remember to press Launch Engine (Cmd-Shift-L) — providers and agents cannot be edited until"
  echo "the engine is running, and both panels now say so rather than leaving a dead button."
fi
