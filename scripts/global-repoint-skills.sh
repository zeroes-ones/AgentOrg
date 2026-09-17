#!/usr/bin/env bash
#=============================================================================
# Repoint GLOBAL agent skill directories to the fresh Skills flat layer,
# preserving personal skills (moomoo*) via per-skill symlinks.
#
# Why this exists: scripts/install.sh deliberately does NOT repoint an existing
# symlink (it reports "already linked"), and it links one symlink to skills-flat,
# which would hide the personal moomoo* skills that live only in the stale store
# (~/.zeroes-ones/skills). This instead makes each agent dir a real directory of
# per-skill symlinks: all library skills + the personal ones.
#
# Non-destructive: a symlink is only replaced when it is a symlink (never a real
# directory with content); real directories keep their content and are added to.
# Agents whose home dir is not installed are skipped (same rule as install.sh).
#
# Usage:
#   bash scripts/global-repoint-skills.sh
#   DRY_RUN=1 bash scripts/global-repoint-skills.sh      # preview only
#
# Env overrides: HOME, FRESH, STALE, PRESERVE, AGENTS, DRY_RUN
#=============================================================================
set -euo pipefail

FRESH="${FRESH:-$HOME/Documents/Projects/Skills}"
FLAT="$FRESH/skills-flat"
STALE="${STALE:-$HOME/.zeroes-ones/skills/skills}"
PRESERVE="${PRESERVE:-install-moomoo-opend moomoo-capital-anomaly moomoo-comment-sentiment moomoo-derivatives-anomaly moomoo-news-search moomoo-stock-digest moomoo-technical-anomaly moomooapi}"
AGENTS="${AGENTS:-agents claude copilot github cursor codex gemini windsurf cline opencode}"
DRY_RUN="${DRY_RUN:-0}"

if [ ! -d "$FLAT" ]; then
    echo "ERROR: flat discovery layer not found at $FLAT" >&2
    echo "       Run 'skills-update' first, or set FRESH=/path/to/Skills" >&2
    exit 1
fi

echo "Fresh layer : $FLAT"
echo "Stale store : $STALE"
echo "Mode        : $([ "$DRY_RUN" = "1" ] && echo 'DRY RUN (no changes)' || echo 'APPLY')"
echo ""

total_lib=0; total_pers=0; linked_agents=0
for a in $AGENTS; do
    home_agent="$HOME/.$a"
    target="$home_agent/skills"

    # Skip agents that are not installed (matches scripts/install.sh semantics).
    if [ ! -d "$home_agent" ]; then
        printf '  %-10s skip (not installed)\n' "$a"
        continue
    fi

    if [ "$DRY_RUN" = "1" ]; then
        kind="realdir"
        [ -L "$target" ] && kind="symlink->$(readlink "$target")"
        printf '  %-10s would convert %s to per-skill links\n' "$a" "$kind"
        continue
    fi

    # Replace only a symlink (ours); never delete a real directory with content.
    if [ -L "$target" ]; then
        rm -f "$target"
    fi
    mkdir -p "$target"

    n=0
    for d in "$FLAT"/*; do
        [ -e "$d" ] || continue
        ln -sfn "$d" "$target/$(basename "$d")"
        n=$((n + 1))
    done

    m=0
    for name in $PRESERVE; do
        if [ -d "$STALE/$name" ] && [ ! -e "$target/$name" ]; then
            ln -sfn "$STALE/$name" "$target/$name"
            m=$((m + 1))
        fi
    done

    total_lib=$((total_lib + n)); total_pers=$((total_pers + m)); linked_agents=$((linked_agents + 1))
    printf '  %-10s -> %s  (%d library + %d personal)\n' "$a" "$target" "$n" "$m"
done

echo ""
if [ "$DRY_RUN" = "1" ]; then
    echo "Dry run complete — re-run without DRY_RUN=1 to apply."
else
    echo "Done. $total_lib library + $total_pers personal symlinks across $linked_agents agent dir(s)."
    echo "Verify: find -L \"\$HOME/.claude/skills\" -maxdepth 2 -name SKILL.md | wc -l"
fi
