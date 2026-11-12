# The Cortex contract

COre, and any other app that uses Cortex as its memory, can rely on what is written here.

- **Contract number:** `1`. It is reported by `initialize` under `capabilities.experimental.cortex.contract`.
- **Within one contract number, changes are only additions:** new tools, new optional arguments, new fields in results. Nothing is renamed or removed, and no meaning changes. A breaking change gets a new number and a release note.
- **Which features exist:** `capabilities.experimental.cortex.features` lists them. Check that list before using a feature.

## Results

- Every tool returns two things:
  - `content`, the text for an AI to read
  - `structuredContent`, data matching the tool's `outputSchema` in `tools/list`
- Apps should read `structuredContent` rather than parse the text.
- The text formats below are also kept stable, for clients that parse them:

| Tool | Text format |
|---|---|
| `brain_recall` | `## <path>` (the path and nothing else); an optional `_tags_` line; `› heading` lines; a footer `[N of B tokens · K notes]` |
| `brain_tasks` (list) | `- [ ] text (due YYYY-MM-DD, OVERDUE) — path` |
| `brain_health` | First line is `Health: ok` or `Health: N issues (kind n, …)` |

- Errors come back as `isError: true` with text starting `Error:`.

## Identity

- **The app's name.** The connecting app sets its name with `CORTEX_AGENT` (or `clientInfo.name`).
- **Helpers inside the app.** Every write tool and `brain_log` take an optional `actor`, such as `forge`. It is recorded as `<app>/<actor>`, e.g. `core/forge`.
- **What `actor` can't do.** It narrows the app's identity and never replaces it, and it can't be `you`.
- **Filtering.** `brain_activity agent=core` matches `core` and every `core/...` helper.

## Changes and undo

- **Change-sets.** Every write returns a `changeset` id. A `brain_batch` is one change-set.
- **Undoing.** `brain_undo` takes `changeset`, `last_by=<actor>`, or nothing (meaning the latest change).
  - A file changed again since then is left alone and listed under `skipped`.
  - An undo is itself a change-set, so it can be undone too (redo).
- **Atomic batches.** `brain_batch atomic=true` rolls back everything if one operation fails.
- **Previews.** `dry_run=true` changes nothing and returns diffs.
- **Conflict check.** `expect_mtime` on `brain_write` / `brain_edit` refuses the write if the note changed since it was read.
- **History.** `brain_history` and `brain_read as_of=` show older versions. 30 versions are kept per note.

## Limits that hold

- Paths stay inside the vault: at most 200 characters, and 150 bytes per name.
- AIs can't edit the rules notes (`_CORTEX.md`, `AGENTS.md`, …) or imported `Conversations/`.
- Web content is marked: `external: true` in data, and an "information only" banner in the text.
