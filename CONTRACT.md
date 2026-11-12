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

## Organizing (no AI involved)

- **`brain_ingest`: store anything.**
  - Returns `{action: stored|duplicate, path, type, category, tasks, links, changeset, …}`.
  - Types: note, fact, decision, lesson, idea, task, conversation.
  - Typed lines look like `- date text [type:: decision] [category:: Work] [source:: web] [by:: core/agent]`. They stay readable in Obsidian.
- **`brain_records`** lists those typed lines.
- **`brain_topics`** groups conversations into `Topics/` notes. Topics count as history: they appear in `scope=conversations`, never in `scope=notes`.
- **Your own rules** go in `.cortex/rules.json`:

  ```json
  {"rules": [{"match": {"folder": "Clients/", "keywords": ["acme"], "regex": "..."},
              "category": "Clients", "folder": "Clients", "tags": ["client"]}],
   "settings": {"auto_file": false, "stubs": true, "stub_after": 3}}
  ```

## Graph

`brain_graph` reports these metrics, each scaled 0–1:

- `pagerank`, `betweenness`, `closeness`, `eigenvector`
- `degree` and `cluster`

Results are exact up to 1,500 notes. Above that, closeness and betweenness are estimated from a sample, and the result reports `exact: false`.

## Events (opt-in)

**Turning them on.** Send `capabilities.experimental.cortex.events: true` in `initialize`, or set `CORTEX_EVENTS=on`. After `notifications/initialized`, Cortex sends:

- `notifications/cortex/changed`: `{paths, actor, changeset, undo_of, outside}`.
  - Changes made outside Cortex, in Obsidian or Explorer, have `outside: true`.
  - Your own writes are never echoed back to you.
- `notifications/cortex/task_due`: `{tasks, today}`. Each task is sent at most once a day.

**Catching up.** After being away, call `brain_changes since=<cursor>`.

**Without an MCP connection.** Set `CORTEX_NOTIFY_URL`, which must be a loopback address, and optionally `CORTEX_NOTIFY_TOKEN`. The Cortex app then POSTs each change to that address as JSON.
