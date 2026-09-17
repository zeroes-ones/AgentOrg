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
# Usage:
#   scripts/run-macos-app.sh              # build (release), bundle, launch
#   scripts/run-macos-app.sh --debug      # a debug build, for a stack trace worth reading
#   scripts/run-macos-app.sh --build-only # assemble the .app without launching
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
  <!-- A regular app: it owns a window, a menu bar and a Dock icon. -->
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
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
