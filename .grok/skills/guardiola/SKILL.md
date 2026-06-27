---
name: guardiola
description: PEP 8 and readability review focused on declarative naming over comments, with optional iterative loop mode. Deletion mode finds bloat to cut. Loop mode improves one file at a time via names and structure — comments only for TODOs or non-simplifiable code. Use for PEP 8 review, PEP 8 ponytail, guardiola, readability loop, declarative naming, simplify style, what can we delete, or when this skill is invoked. Use when the user runs /guardiola.
---

Two modes. Pick based on user intent:

- **Deletion mode** (default): review diffs for PEP 8 bloat — what to cut or shrink. One line per finding. Does not apply fixes.
- **Loop mode**: iterative readability improvements, one file at a time. Prefer renaming over commenting. Suggests small high-impact changes; user applies and pastes back.

## Core philosophy (both modes)

**Names carry intent. Comments are a last resort.**

1. Prefer declarative names for functions, variables, and constants so code reads without narration.
2. Functions should be obvious from their name + signature; split or rename before adding a docstring.
3. **Allowed comments only:**
   - `TODO` / `FIXME` / `HACK` with a concrete next step
   - Non-obvious domain constraints that cannot be expressed as a name (e.g. external API limits, hardware quirks)
   - Workarounds that need a link or ticket reference
   - Tooling directives (`# noqa`, `# type: ignore`) — no prose
4. **Delete on sight:**
   - Section banner comments (`# --- Spotify ---`)
   - Docstrings that restate the function or class name
   - Inline comments explaining what the next line obviously does
   - Comments that duplicate a variable name (`user_id  # the user id`)
5. When a comment explains a name, **rename** instead of commenting.

## Deletion mode

Review diffs for unnecessary PEP 8-related complexity and style bloat. One line per finding: location, what to cut, what replaces it. Goal: shorter code that still follows PEP 8.

### Format

LINE: TAG what. replacement.

or `file:LINE: ...` for multi-file diffs.

Tags:

- **delete**: dead style code, unnecessary blank lines, redundant formatting constructs, speculative style rules, **noise comments**. Replacement: nothing or minimal.
- **pep8**: direct PEP 8 violation that makes code longer or uglier. Show the shorter compliant version.
- **import**: bloated import blocks, wrong grouping, or unnecessary `from x import` sprawl. Name the simplification.
- **name**: vague or abbreviated names; or overly long names. Replace with a declarative name that removes the need for a comment.
- **shrink**: same intent, fewer lines (dict/list comp for style, removing unnecessary parens/whitespace, combining trivial statements).
- **yagni**: style abstraction, config class, or extensible formatting layer with one caller/use.

### Examples

```
L12: delete: three blank lines before def. One blank line is enough.
L4: delete: section banner "# --- Helpers ---". Group with blank line only.
L42: delete: docstring "Returns the user balance." on get_user_balance(). Name is enough.
L87: pep8: line too long (92 chars). Shorten to 79 chars max or break cleanly.
L31: name: get_user_account_balance_after_tax_deduction. user_balance().
L55: shrink: 8-line manual dict build. {k: process(v) for k, v in items}, 1 line.
L19: yagni: StyleConfig class with one use site and two hardcoded values. Inline the 4-space indent.
L68: delete: inline comment "# increment counter" on count += 1. Obvious from code.
```

### Workflow

1. Identify the diff or files to review (uncommitted changes, a branch, or paths the user names).
2. Scan for PEP 8 bloat, noise comments, and names that force unnecessary comments.
3. Emit one line per finding using the format above.
4. End with net reduction: `Net reduction: N lines possible.`
5. If nothing worth cutting: `Lean already. Ship.` and stop.

### Scoring

End with the only metric that matters: net reduction of N lines possible.

If there is nothing worth cutting on style grounds: **Lean already. Ship.** and stop.

### Boundaries

Scope: PEP 8 style, formatting, naming, imports, whitespace, comments, and structural bloat **only**.

Correctness bugs, logic errors, security, performance, and missing features are explicitly out of scope. Route them to a normal review.

Reasonable blank lines for readability and a single smoke test or `assert` are **not** bloat — never flag them for deletion.

Does not apply fixes. Only lists what can be removed or simplified.

"stop ponytail", "normal mode", or "verbose review": revert to full explanatory style.

## Iterative Readability & Transparency Loop

Activate when the user says things like: "readability loop", "transparency iteration", "mejora legibilidad un archivo a la vez", "loop de mejora", "iterative readability review", "enfocado en legibilidad y transparencia", or "start improvement loop on this file".

Behavior in loop mode:

- Work on **exactly one file per iteration**.
- Primary goals: **declarative naming** and **obvious function flow** — a reader understands intent from names and structure, not comments.
- **Comment budget:** zero new comments unless TODO or genuinely non-simplifiable. Remove existing noise comments before adding anything.
- Each iteration:
  1. User pastes the current version of the file (or names a path).
  2. Review for naming, structure, and leftover noise comments.
  3. Suggest 2-4 high-impact changes. Prefer: rename symbol, extract/rename function, delete comment, collapse redundant docstring.
  4. For each suggestion: location + rename/delete + why the code reads clearer without the comment.
  5. End with a short progress note + next step: "Apply these, paste the new version and say 'next iteration'".
- Stop when the user says "loop complete", "stop loop", "suficiente", or the file reads clearly without narration.
- Never do big refactors in one go. Small, safe, understandable steps.

Loop mode complements deletion mode. Deletion mode shrinks code. Loop mode makes code self-explanatory through names.