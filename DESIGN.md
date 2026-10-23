# Design direction — HiveSplit (calm-precise, CLI)

HiveSplit is a terminal product. This file is its design source of truth: the
tokens below are what every rendered block of output is measured against, and
`hivesplit.py` implements them in the renderer section
(`COLUMN_GAP`, `render_table`, `render_fields`, `WELCOME`).

## Surface

| Token | Value | Used by |
| --- | --- | --- |
| Surface / mode | **terminal dark** (the only honest CLI surface) | every command |
| Accent (one) | `oklch(0.58 0.12 210)` → `#008ca1` | primary status emphasis only |
| Neutral ramp | terminal foreground `#d6dde0`, muted `#8b979c`, rule `#3a4448`, background `#0e1416` | text, secondary text, separator rules |
| Body contrast | fg on bg ≈ 12:1, muted on bg ≈ 5.2:1 — both clear WCAG AA | all output |

The accent is declared once for the record. HiveSplit prints **no ANSI colour
at all**: the product contract is "clean & minimal terminal output, no colour,
no box drawing", so status is always carried by a word (`low` / `elevated` /
`high`, `active` / `sold` / …) and never by hue — the accent marks the primary
status line in the product's own documentation and any future rich surface,
and CLI output stays pipe-safe and dye-free.

## Type

* Family: **system monospace** (inherited from the terminal).
* Personality: mono / mono — one family, no display face.
* Ramp:
  * block heading — plain sentence case, one line, then a blank line
  * column header — `UPPERCASE`
  * body / values — as stored
  * empty value — `—` (em dash)
  * secondary detail — inline in parentheses after the value, e.g. `(20d ago)`

## Density & shape

* Compact: one blank line between blocks, never two.
* Columns: content-sized — each column padded to its widest value, two spaces
  (`COLUMN_GAP`) between columns, right-aligned numerics, no truncation of the
  table itself (long free text is truncated to 34 characters with `…`).
* Separators: a single ASCII hyphen rule under each column header. Box-drawing
  glyphs and frames are deliberately not used — the brief forbids them and they
  do not survive every terminal font.
* No chrome: no banners inside command output, no progress spinners, no
  decoration. The one exception is the bare-invocation welcome screen, which is
  the product's own "help first" landing block.

## Navigation skeleton (help-first, not top-nav + card-grid)

```
hivesplit
├── init            create the store, set the passphrase
├── unlock          open a 12-hour write session
├── hive            add | list | show | status
├── split           record a child hive + its parent edge
├── tree            lineage as an indented, date-labelled tree
├── queen           add | list | show
├── inspect         add | list
├── risk            score, band, per-factor breakdown
├── status          overdue / never inspected / check first
└── export          json | csv → stdout or --out
```

Read commands run while locked; writes and `export` need a live session. The
tree view is the product's signature surface: two-space indent per generation,
each line carrying label, id, kind, status and the split date that produced it.
