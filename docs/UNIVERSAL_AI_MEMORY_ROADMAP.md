# Universal AI Memory: roadmap

**Status: planned, not built.** Nothing on this page exists yet, except what's labelled **exists** (things Cortex already does today, as described in the [README](../README.md)).

**When work starts:** only after COre v13.0.0 ships. The first step is a fresh audit of Cortex (C0), because the code will have moved on by then. A phase counts as done only when its acceptance checks pass and the evidence (test runs, commits, PRs) is linked here. Having the code isn't enough.

## The goal

Cortex already gives Claude Code, Codex, Gemini CLI, Cursor and Claude Desktop **one shared brain on your computer**. The next step is a brain that:

- **Keeps every AI conversation as an untouched original record**, separate from the curated knowledge drawn from it.
- **Records where every piece of knowledge came from:** which AI, which model, which conversation and which message, and whether it was a proposal, a decision or verified work.
- **Lets any connected AI read and add to it through one provider-neutral contract** (MCP first, plus a plain API), so a decision made in a ChatGPT chat can be found from Claude, and Claude's implementation record from ChatGPT.
- **Can be reached from your phone while your Windows laptop is switched off**, through an authenticated service you own and switch on yourself.
- **Stays local-first and portable:** plain Markdown that opens in Obsidian, with no account, no cloud and no remote access unless you turn them on.

Cortex is the memory engine in a larger setup: COre is the main agent, a Memory Gateway connects outside AI providers, and Obsidian is where you read and edit. This page covers the Cortex side. COre's part is a later, separate phase (C9).

## Principles

| Principle | What it means in practice |
|---|---|
| Cortex stays deterministic | No language model inside Cortex's core. Indexing, provenance, dedupe, conflict detection and lifecycle rules are plain code that can be tested. Optional extras (like meaning search through Ollama) stay optional. |
| Provider logic stays outside the core | Each AI provider gets its own adapter in the gateway layer. Cortex only sees one normalized event format. |
| Local first | Cortex keeps working fully offline on one computer. Remote access is an opt-in mode, never the default. The app keeps listening only on `127.0.0.1` unless you choose otherwise. |
| Portable and inspectable | Markdown stays the human-readable form. Indexes and caches can always be rebuilt from the authoritative records. |
| No invented capabilities | Every provider adapter says exactly how it captures (see C3). If a provider has no official way to read its chats, Cortex says so instead of pretending. |
| Claims aren't facts | Proposal, recommendation, decision and verified implementation are different states. An AI saying it did something is not proof it did. |
| Nothing is lost silently | Originals are never rewritten. Conflicting versions are both kept and shown. Deletes are recoverable until you purge them. |

## What already exists to build on

| Exists | Used by |
|---|---|
| Plain Markdown vault, Obsidian compatible | Every phase |
| MCP tools: `brain_context`, `brain_recall`, `brain_search`, `brain_read`, `brain_write`, `brain_edit`, `brain_batch`, `brain_move`, `brain_tasks`, `brain_capture`, `brain_log`, `brain_activity`, `brain_recap`, `brain_health`, `web_research`, `web_fetch` | C3 (the exchange contract reuses them) |
| Importers: Claude Code, Codex CLI and Gemini CLI read automatically every 5 minutes; Cursor experimental; ChatGPT and Claude.ai through their data export | C1, C3 |
| Imported conversations already record the app, session id, project, start and update times, and per-message times. AIs can't edit them. | C1 |
| Secret redaction on import; invisible and bidi characters stripped | C1, C7 |
| Version history (last 30 per note), `.trash`, verified daily backups and restore into a new folder | C2, C4 |
| A shared logbook, one file per computer | C3, C4 |
| A save is refused if the note changed since it was opened | C4 |
| Health lists sync conflicts (Google Drive and Syncthing conflict copies) | C4 |
| Web content labelled `trust: external`; protected rule files (`_CORTEX.md`, `CLAUDE.md`, …) | C7 |
| Google Drive mirror sync, with the search index kept local | C4 |

**Gaps this roadmap closes:**
- Imports don't record the model, message ids, a content hash, attachments or how they were captured.
- Transcripts are cleaned (tool output dropped), so the original isn't kept.
- Knowledge has no source links, verification status or lifecycle.
- Providers other than the built-in importers have no way to send data in.
- There is no authenticated remote access.
- There is no phone access while the laptop is off.
- Concurrent writers have no single revision protocol.
- There are no per-agent or per-device permissions, and no way to revoke them.

## How the data is layered

```
1. Append-only event log        every ingest, edit, decision and status change, with its source
2. Conversation archive         original messages, never rewritten (content hash per message)
3. Knowledge records            decisions, discoveries, specs, tasks, lessons, entities ... each linked to its sources
4. Markdown view                what you and Obsidian read and edit
5. Search and graph indexes     rebuildable from 1-3, kept local on each device
6. Device caches                phone and laptop offline copies, clearly marked as cached
```

Layers 1–3 are authoritative; layers 4–6 are derived from them. Active SQLite databases, locks and temporary files are never synced.

## Phases

Each phase lists what it delivers, what it reuses, how it is accepted, and its main risks. The phases run in order unless a row says otherwise.

### C0 — Fresh audit and architecture map
- **Delivers:**
  - an audit of Cortex at that time
  - a map of every component against the layers above
  - a migration plan for existing vaults
  - **a verified list of what each provider really allows:** official APIs, connectors, local session logs, exports. Each entry has a date and a source, and marks what can't be done.
  - a cost and infrastructure estimate for the remote mode
- **Accepted when:** the audit and provider table are committed under `docs/` and every capability claim has a source.
- **Risk:** provider capabilities change often. The table gets a "last checked" date and a re-check in every later phase.

### C1 — Provenance and the conversation archive
- **Delivers:**
  - **A record for each message with:** provider, model (when known), conversation id, message id, role, original content, timestamp, project, source reference, attachments (by reference and hash), capture method and content hash.
  - **The original is stored once and never rewritten.** The readable transcript note (the current `Conversations/<App>/` format) becomes a view of it.
  - **Redaction stays the default.** Any redaction is recorded, so you can always see that something was removed.
- **Reuses:** the existing importers and their incremental state; redaction; protected conversation notes.
- **Accepted when:**
  - Re-importing the same session creates no duplicates (same hashes).
  - Changing an archived file is detected.
  - The existing vault migrates with a reversible migration and no lost notes.
- **Risk:** keeping the original grows storage (today's transcripts are 10–150× smaller because tool output is dropped). C0 decides what "original" means for each source, and the size limits.

### C2 — Knowledge records and their lifecycle
- **Delivers:**
  - **A record kind:** proposal, recommendation, decision, verified implementation, superseded or unresolved contradiction.
  - **A lifecycle:** Captured → Processed → Candidate → Verified → Archived.
  - **Each record carries:** its source links (back to C1 messages), creation time, provider and model, verification status, confidence where it applies, and project or topic.
  - **You can correct, delete, recover and set retention for any record.**
  - **Rules:**
    - A recommendation never becomes a decision without a decision event.
    - Nothing is marked verified without evidence (a commit, a test run, a file, or your confirmation).
- **Reuses:** frontmatter, `/mcp__cortex__decide` (which already flags conflicting decisions), version history, `.trash`.
- **Accepted when:**
  - Unit tests cover every state change.
  - An AI can't move a record to Verified without attaching evidence.
  - Superseded records stay findable and are labelled.

### C3 — Provider-neutral ingestion and the exchange contract
- **Delivers:**
  - **A versioned contract** that every connected AI uses through MCP (and later a plain API).
  - **Existing tools are reused wherever they already do the job.** Only the missing operations are added.

    | Contract operation | Cortex today |
    |---|---|
    | `memory_search` | `brain_search` (exists) |
    | `memory_recall` | `brain_recall` (exists) |
    | `memory_get_project_context` | `brain_context` (exists, extended with project scope) |
    | `memory_get_recent_changes` | `brain_recap` (exists) |
    | `memory_get_agent_activity` | `brain_activity` (exists) |
    | `memory_get_conversation` | new: an original conversation from the C1 archive |
    | `memory_get_decisions` | new: decision records with sources (C2) |
    | `memory_ingest` | new: normalized events from a gateway adapter |
    | `memory_publish_event` | new: discoveries, recommendations, results, test results, blockers |
    | `memory_get_project_status` | new: progress built from verified records |

  - **Adapters declare their capture mode:** real-time, near-real-time, periodic, export-based or manual import. The app shows each provider's mode and limits.
  - **Delivery is durable:** a persistent queue with retry, and dedupe by content hash.
- **Starting adapters** (to be confirmed in C0):

  | Provider | Current mode |
  |---|---|
  | COre | Real-time, through its memory gateway (C9) |
  | Claude Code, Codex CLI, Gemini CLI | Periodic, from local session logs (exists) |
  | ChatGPT, Claude.ai | Export-based (exists) |
  | Anything else | Only what an official route allows |

- **Accepted when:**
  - Two adapters ingest the same conversation and only one copy is kept.
  - Killing the process mid-ingest loses nothing and duplicates nothing after a restart.
  - Existing MCP tools keep working unchanged for current users.
- **Risk:** some providers offer no live capture. That is shown to the user, not hidden.

### C4 — Revision-aware sync, conflicts, backup and recovery
- **Delivers:**
  - **One authoritative write protocol.** Every write names the revision it was based on. A write on a stale revision becomes a recorded conflict, never an overwrite.
  - **Conflicts are preserved.** Both versions and both sources are kept and listed until you resolve them. This applies to contradictions between AIs too.
  - **Sync providers:** Markdown can still sync through Google Drive (exists) or Syncthing. The app warns if two sync providers point at one vault.
  - **Backups:** the existing verified backups cover the archive and the event log.
- **Reuses:** the stale-save refusal, Health's conflict list, the per-computer logbook, backup and restore.
- **Accepted when:**
  - Two devices editing the same record produce a visible conflict with both versions.
  - A restore from backup matches the checksums.
  - Indexes rebuild from the authoritative records alone.

### C5 — Authenticated always-on Cortex (opt-in)
- **Delivers:**
  - **An always-on Cortex service** that you switch on and host somewhere you control (options and their monthly costs are listed in C0), so memory stays reachable when the laptop is off.
  - **Security:**
    - TLS for every connection
    - a separate credential for each device and each provider
    - read and write permissions per project and per agent
    - an audit log of every access
    - revoke any device or provider in one step
  - **No endpoint works without authentication.**
- **Coordination:** the remote service coordinates writes using the C4 protocol. The laptop is never required as a sync server.
- **Accepted when:**
  - Unauthenticated and revoked requests are refused (automated tests).
  - Memory is readable while the laptop is off.
  - Local-only mode still works exactly as before.
- **Risk:** this is the first mode where data leaves your computers by design. It stays off until you enable it, and the README's privacy section is updated before it ships.

### C6 — Phone access while the laptop is off
- **Delivers:**
  - **From the phone, against the C5 service:** search memory, read conversations from any AI, see project status and agent activity, and send in ideas and notes.
  - **Offline:**
    - Previously synced data stays readable and is clearly marked as cached.
    - New submissions are queued and shown as **queued**, never as saved, until the service confirms them.
- **Accepted when:**
  - With the laptop powered off, the phone retrieves a decision recorded from another AI, with its source.
  - A note submitted offline arrives exactly once after reconnecting.

### C7 — Security and privacy hardening
- **Delivers:**
  - **Sharing:**
    - Capture exclusions: per provider, project or conversation.
    - Private by default: a conversation isn't shared with every agent automatically.
  - **Data handling:**
    - Sensitive-data redaction rules.
    - Retention periods.
    - A true purge: deletion beyond `.trash`, with a record that a purge happened.
    - Protected local credentials.
  - **Red-team tests:**
    - prompt injection through ingested conversations
    - forged provenance
    - replayed events
    - a revoked device trying again
- **Reuses:** the existing security table (SSRF, path checks, protected rule files, `trust: external`, redaction). Every new threat gets an automated test that tries the attack, as today.

### C8 — Multi-agent exchange, proven
- **Delivers:** end-to-end proof that knowledge moves between AIs through official routes only (MCP or the providers' supported connectors, as verified in C0):
  - Claude reads a decision first made with ChatGPT.
  - ChatGPT reads Claude Code's implementation record.
  - Gemini adds research that both can see.
- **Accepted when:** the acceptance tests below pass, with the evidence linked.

### C9 — COre integration (later, separate phase)
- **Delivers:**
  - **COre's memory gateway adopts the C3 contract.** COre conversations are captured in real time.
  - **The COre cloud runtime, the mobile app and the local/cloud execution bridge** use the C5 service. They never claim to run a desktop action while the laptop is offline.
- **Where it's tracked:** in the COre repository. It is listed here only so the Cortex side leaves room for it.

## Acceptance tests

Every test is labelled with the tier it was run at:
- **local**: one computer
- **remote**: the C5 service
- **real phone**: an Android phone with the laptop powered off

| # | Test | Phase | Tier needed |
|---|---|---|---|
| 1 | Discuss a COre architecture decision with ChatGPT. Cortex stores it with provenance. Claude retrieves it with the original decision and its source. | C3, C8 | remote |
| 2 | Claude Code implements a feature. The real code changes, commit and test results are recorded as a verified implementation. ChatGPT retrieves that history. | C2, C8 | remote |
| 3 | Switch the Windows laptop off. From Android, search shared memory and read a conversation from another AI. | C5, C6 | real phone |
| 4 | Capture a note or conversation on the phone. It reaches the memory pipeline without the laptop. | C6 | real phone |
| 5 | Switch the laptop back on. It receives the new memory, with no overwrites and no lost edits. | C4 | remote |
| 6 | Two AIs record contradictory facts. Both are kept with their sources and flagged as a contradiction. Neither is silently chosen. | C2, C4 | local |
| 7 | Cut a provider off during ingestion. Pending records recover with no duplicates and no silent loss. | C3 | local |
| 8 | Revoke a device or provider. Its credentials no longer reach any protected endpoint. | C5, C7 | remote |

## Design note for future Cortex UI work

When the app grows the new views (archive, provenance, conflicts, agent timeline), they follow the shared COre direction:
- **Look:** dark-first, calm and archival; graph views where they help.
- **Colour:** deep purple (`#7C5CFF`) for COre's identity and teal (`#3DF0D0`) for memory and system state, on near-black surfaces (`#07080D`, `#10121B`).
- **Type:** Inter-style sans-serif, with monospace only for identifiers.
- **Avoid:** fake activity indicators and decorative effects.

Obsidian keeps its own editor. Cortex doesn't try to replace it.

## Decisions needed before C5

1. **Where the always-on service runs, and the monthly budget for it.** C0 will list concrete options with costs.
2. **Which providers come first after COre and Claude Code.** This depends on what C0 finds each one really allows.
3. **One sync provider per vault (Google Drive or Syncthing), never both.** Which one is the default?
