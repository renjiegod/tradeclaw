# Skill Conventions

This file documents the conventions that the `.doyoutrade/skills/` directory
follows on top of the upstream `skill-creator` guidance. It is *not* enforced
by code today — `doyoutrade/skills/parser.py` only consumes `name`,
`description`, and `license`. The conventions exist so humans (and future
routing tools) can scan the directory predictably.

Whenever you add or modify a skill, follow these rules. Whenever you find a
skill that violates them, either fix the skill or update this file.

## Directory layout

```
.doyoutrade/skills/
├── CONVENTIONS.md                       # this file
├── <skill-name>/                        # one directory per skill (kebab-case)
│   ├── SKILL.md                         # required — YAML frontmatter + body
│   ├── references/                      # optional — long-form lookup docs
│   │   └── *.md
│   ├── scripts/                         # optional — executable helpers
│   └── examples.md                      # optional — worked examples (legacy)
└── skill-creator/                       # upstream meta-skill, treat as vendor
```

- **Directory name = `name` in frontmatter.** Both must be kebab-case
  (`daily-range-swing-trade`, not `daily_range_swing_trade`). The directory
  name is what the slash-command resolver normalizes against
  (`doyoutrade/assistant/slash_commands.py:_normalize_skill_key`).
- Keep `SKILL.md` under ~500 lines. When it grows past that, push detailed
  reference / payload / error-code material into `references/<topic>.md` and
  add a `## References` section to `SKILL.md` pointing at each file with a
  one-line "load when you need …" hint. See `strategy-authoring/` for the
  template.

## Frontmatter

```yaml
---
name: kebab-case-name              # required, must match directory name
description: ...                   # required, see "Description style" below
category: strategy                 # optional, see "category" below
style: process                     # optional, see "style" below
---
```

The parser only reads `name`, `description`, and `license`. Other keys are
free-text human metadata.

### `category` (what the skill is *about*)

| Value | When to use |
|---|---|
| `strategy` | The skill is centred on Doyoutrade strategy authoring, lifecycle, or evidence (definition / task / backtest / iteration). |
| `analysis` | The skill wraps a specific analysis subcommand (`doyoutrade-cli analysis pattern`, `doyoutrade-cli analysis factor`) — i.e. it tells the agent how to drive a specific CLI command. |
| `tool` | The skill wraps a workflow against a runtime artifact but isn't backed by a single CLI subcommand (e.g. `backtest-diagnose` is a recovery procedure over `doyoutrade-cli backtest run` / `backtest summary` outputs). |
| `reference` | Pure lookup card with no tool to drive — formulas, field shapes, allowed imports, glossary. |

### `style` (how the body reads)

| Value | Pattern | Example |
|---|---|---|
| `process` | Ordered workflow / checklist. The reader follows steps top-to-bottom. | `strategy-authoring`, `backtest-diagnose`, `strategy-iteration` |
| `recipe` | One worked scenario, end-to-end, with verifiable conditions. The reader adapts the scenario rather than the order. | `daily-range-swing-trade`, `factor-research` |
| `reference` | Flat tables / formulas / cards. The reader jumps to the relevant row, not the top. | `technical-basic`, `strategy-definition-authoring/references/*` |
| `template` | Reusable scaffolding the reader copies and customises. | (none active — formerly used by the indicator skills now folded into `pattern-recognition` / `technical-basic`) |

Most skills will pair a `category` with a `style` (e.g. `category: strategy`
+ `style: process`). Pick the closest fit; don't invent new values without
adding them here first.

### Optional `license`

Only set `license` when the skill ships under a license other than the
repo's default. The parser supports the field (`Skill.license`). As of
2026-05-16 no skill carries this field — leave it out unless you have a
real reason to add it.

## Description style

The `description` field is the **primary trigger mechanism** — Claude
decides whether to load `SKILL.md` based on this string and the skill name
alone. Two things must be true:

1. It explains *what the skill does* in concrete terms (which tool it
   wraps, which workflow it codifies).
2. It lists *when to invoke it*, including bilingual user-utterance
   examples (Chinese + English) when the skill is likely to be triggered by
   conversational phrasing.

Reuse the pattern:

> *<one-clause "what it does">* — *<one-clause "what makes it specific">*.
> Use this skill whenever the user *<verb cluster>*, asks *"…example
> phrasing in two languages…"*, or *<implicit-trigger scenario>*.
> Companion to *`peer-skill-name`* *(when applicable)*.

Examples in the directory: `backtest-diagnose`, `factor-research`,
`strategy-iteration`. Avoid bare descriptions like
"Factor research framework with IC/IR analysis" — they don't trigger when
the user asks "is this factor any good".

## Cross-skill routing

When a skill is one node in a multi-step workflow, surface routing hints in
**two** places:

1. The `description` field — name the companion skills the agent should
   prefer next (`Companion to backtest-diagnose / strategy-iteration`).
2. An HTML comment at the top of the body for human readers
   (`<!-- Routing: writing strategy code → strategy-definition-authoring -->`).

This keeps both the auto-triggering layer and the human-reading layer in
sync.

## References folder pattern

When a skill grows beyond ~200 lines or starts carrying tool-call payloads /
error-code dictionaries, follow this layout:

```
my-skill/
├── SKILL.md                       # workflow + contract + guardrails
└── references/
    ├── payload-shape.md           # exact field-by-field schema
    ├── error-codes.md             # lookup for structured errors
    └── …
```

In `SKILL.md`, add a final `## References` section that follows this exact
shape — `strategy-definition-authoring/SKILL.md` is the canonical example:

```markdown
## References

Deep-dive files in this skill's `references/` folder. Load the file that
matches the gap rather than reading everything up front.

- [`references/foo.md`](references/foo.md) — one-line "what's in it".
  **Read this when <concrete trigger>.**
- [`references/bar.md`](references/bar.md) — one-line "what's in it".
  **Read this when <concrete trigger>.**
```

Three rules the template enforces:

1. **Clickable markdown link**, not bare backticks. Humans browsing the repo
   click through; the agent reads the same path. Format:
   `` [`references/foo.md`](references/foo.md) ``.
2. **One-line summary + bold "Read this when …" trigger.** The trigger is
   what makes the agent actually load the file — keep it concrete (an
   `error_code`, a tool name, a phase like "before calling X"), not vague
   ("when you need details").
3. **No inline reference content in `SKILL.md`.** The agent reads the
   lightweight skeleton on every invocation and descends into a reference
   only when it needs that depth. Burying reference material in `SKILL.md`
   defeats the progressive-disclosure pattern.

### How agents actually read these files

The doyoutrade assistant reads `references/*.md` via the `read_file` tool
(`doyoutrade/tools/file_tools.py`, `ReadFileTool`), **not** an editor-style line reader:

- `offset` / `limit` are **byte offsets**, not line numbers. Do not write
  "see line 42" in references — use `## Section Headings` and grep-friendly
  anchors instead.
- Default `limit` is 50 000 chars; the hard ceiling (`MAX_TEXT_LENGTH`) is
  100 000 chars (~25–30k tokens for mixed CN/EN). Files comfortably under
  that today; keep an eye on it as references grow.
- Output has no `cat -n` line numbers — references must be navigable by
  section heading + lookup table.

## When in doubt

- Smaller is better. If a skill could be a section of a sibling skill,
  combine them.
- A reference card and a workflow card usually serve different readers —
  keep them separate (e.g. `technical-basic` is a formula card distinct
  from the `strategy-definition-authoring` workflow). But when a "reference"
  card is forced-loaded as a prerequisite by exactly one workflow card,
  fold it into the workflow's `references/` folder instead — that's how
  `strategy-definition-authoring` absorbed the former `strategy-sdk-cheatsheet`
  in 2026-05.
- The upstream `skill-creator/` directory is the meta-tool for evolving
  these skills; respect its docs (`skill-creator/SKILL.md`) when running
  evals / iterations.
