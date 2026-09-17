# Agent — Skills setup

This workspace loads the [zeroes-ones/Skills](https://github.com/zeroes-ones/Skills)
library (327 skills) two ways: **project-scoped** (this folder) and **global**
(every project).

## Project scope — done

`reasonix.toml` declares the library roots for Reasonix:

```toml
[skills]
paths = [
  "/Users/sp.vm/Documents/Projects/Skills",
  "/Users/sp.vm/Documents/Projects/Skills/skills",
]
```

> A project-level `[skills]` table **replaces** the global one rather than
> merging, so the roots are repeated here for parity with
> `~/.reasonix/config.toml`.

Cross-agent links (created with the library's canonical installer) expose the
flat discovery layer to every agent that reads skills one level deep:

```bash
SKILLS_HOME=/Users/sp.vm/Documents/Projects/Skills \
  bash /Users/sp.vm/Documents/Projects/Skills/scripts/init-project.sh .
```

That links `.agents/skills`, `.claude/skills`, `.copilot/skills`,
`.github/skills`, `.cursor/skills`, `.codex/skills`, `.gemini/skills`,
`.windsurf/skills`, `.cline/skills`, and `.opencode/skills` →
`…/Skills/skills-flat`, and records the ignored paths in `.gitignore`.

Verify:

```bash
reasonix doctor capabilities --json     # expect ~327 library skills
skills-init --status                    # tier + linked count
```

## Global scope — one manual step left

Reasonix already discovers all 327 skills globally via `~/.reasonix/config.toml`.

The **cross-agent** global dirs (`~/.claude/skills`, `~/.copilot/skills`,
`~/.cursor/skills`, `~/.gemini/skills`) still point at the stale clone at
`~/.zeroes-ones/skills` (311 skills, predates the flat layer). Repointing them
requires writing outside this sandbox, so run this from a normal shell:

```bash
bash scripts/global-repoint-skills.sh          # preview with DRY_RUN=1 first
```

The script converts each agent dir into per-skill symlinks — the 327 library
skills **plus** your personal `moomoo*` skills that live only in the stale store
— without deleting anything.
