# Cortex

**One second brain shared by all your AIs.** Claude Code, Codex, Gemini CLI, Cursor and Claude Desktop all read and write the same notes, and you get an app to see and edit everything they remember.

- **Your notes stay yours.** Everything is plain Markdown in a folder, so it opens in Obsidian too.
- **One brain for every AI.** A single MCP server connects every AI app. One AI saves a decision and the next one already knows it.
- **A shared logbook.** You can see which AI changed what, and when.
- **Undo anything.** Every change by you or any AI keeps the previous version (the last 30 per note). Restore one with a click.
- **Tasks from everywhere.** Every `- [ ]` checkbox in any note shows up in one list, with due dates and overdue warnings.
- **Self-maintaining.** The health check finds broken links, duplicates, stale projects, overdue tasks and orphan notes. Renaming a note fixes every link to it.
- **Search by meaning (optional).** With [Ollama](https://ollama.com) running, "car repairs" also finds a note about "fixing the automobile". It runs on your machine, and without it keyword search still works.
- **Fast.** On a 5,000-note vault: search 10ms, save (with a version snapshot) 1ms, full health check 100ms, re-check for changed files 80ms.
- **Nothing to install** except Python 3.10+. It uses no packages, no cloud and no account. The app only listens on your own computer.

**What's next:** the [Universal AI Memory roadmap](docs/UNIVERSAL_AI_MEMORY_ROADMAP.md) covers provenance for every AI conversation, one memory contract for every AI provider, and opt-in phone access while your PC is off. It is planned, not built yet.

## Install (Windows): Cortex.exe

No Python needed.

1. Download **`Cortex-windows.zip`** from this repo's [Releases](../../releases), or from the latest successful build under [Actions → Build Windows app](../../actions/workflows/build-windows.yml).
2. **Right-click the zip → Properties → tick "Unblock" → OK**, then extract it somewhere permanent, for example `Documents\Cortex app`. Your AI apps will point at this folder, so don't move it afterwards.
3. Double-click **`Cortex.exe`**. The first time, it creates your brain in `Documents\Cortex` and opens the app.
4. On the Home page, click **Connect my AI apps**, and choose whether to **import your AI history**. Then restart your AI apps and ask one: *"What's in my brain?"*

**"Windows protected your PC"?** Cortex is free and not code-signed (a signing certificate costs a few hundred dollars a year), so Windows SmartScreen doesn't recognise it yet. Click **More info → Run anyway**. You only need to do this once.

**Antivirus warning?** Cortex reads other AI apps' chat history (only after you click *Yes, import*), recognises API-key patterns (to redact them), and edits AI apps' settings (when you click *Connect*). On paper that resembles spyware, so some scanners flag it. Don't just trust that, check it:
- Compare the zip's SHA-256 with `Cortex-windows.zip.sha256.txt` from the same release (`certutil -hashfile Cortex-windows.zip SHA256`).
- Upload it to [virustotal.com](https://www.virustotal.com). A lone vague detection (`Wacatac`, `!ml`, `Generic`) means a false positive.
- Report false positives to your antivirus vendor (Microsoft: [wdsi/filesubmission](https://www.microsoft.com/en-us/wdsi/filesubmission)).
- The full source is in this repo, and every `.exe` is built from it by GitHub on its own Windows machines (see the workflow), never on someone's PC.

**From source instead:** install [Python](https://www.python.org/downloads/) (tick "Add python.exe to PATH") and double-click `Cortex.bat`.

## Install (Mac / Linux)

```bash
python3 run.py init              # or: python3 run.py init ~/path/to/your/vault
python3 run.py connect           # plugs in Claude Code, Codex, Gemini, Cursor, Claude Desktop
python3 run.py app               # opens http://127.0.0.1:7777
```

## Local + cloud (Google Drive)

Keep the brain on your PC **and** in Google Drive. Cortex works on the local copy at full speed, offline too, and Google Drive for desktop uploads every change and keeps all your computers in step.

**Set it up once:**
1. Install [Google Drive for desktop](https://www.google.com/drive/download/) and sign in.
2. In its settings, under **My Drive syncing options**, choose **Mirror files**. This keeps a full offline copy, which Cortex needs to search everything instantly.
3. Close Cortex and run:
   ```
   python run.py cloud
   ```
   It finds your Google Drive, copies the brain into `My Drive/Cortex`, **checks every file by checksum**, switches over, reconnects your AI apps and makes the first backup. Your old folder is left untouched as a fallback.

**On another computer:** install Google Drive for desktop and Cortex, then run `python run.py init "<path to My Drive/Cortex>"` and `python run.py connect`. Both computers now share one brain.

What's synced and what isn't:

| Synced through Google Drive | Kept local on each computer |
|---|---|
| All notes, imported conversations, research | The search index (a database file in a synced folder gets corrupted, and it rebuilds from the notes anyway) |
| Version history | Which AI conversations this computer has already imported |
| The AI logbook, as **one file per computer**, so two PCs never edit the same file | |

**Backups.** Sync copies mistakes too: a deleted note disappears everywhere. So Cortex also saves a **verified daily snapshot** (a zip with a checksum for every file) to `My Drive/Cortex Backups`. It keeps the last 30 days plus one per month before that. **Sync & backup → Back up now** makes one on demand. To go back in time:
```
python run.py restore "My Drive/Cortex Backups/Cortex-backup-20261108-090000.zip" "C:\Users\you\Documents\Cortex restored"
```
Restore always goes into a new folder and never overwrites your brain. A damaged or tampered backup is refused.

**If two computers edit the same note at once**, Google Drive keeps both versions. Cortex's **Health** page lists these sync conflicts so you can merge them.

**Privacy.** With cloud sync on, your notes and imported AI conversations are stored in your Google account. They're protected by your Google sign-in, but Google holds them. Obvious secrets are already redacted when conversations are imported. If you'd rather keep everything on one machine, don't run `cloud`: Cortex works exactly the same locally.

**Bonus:** with your Google AI Pro plan, Gemini can read files in your Drive, so you can also ask Gemini on the web or your phone about notes in `My Drive/Cortex`.

## Using the app

| Do this | How |
|---|---|
| Search everything | **Ctrl+K**, then type. Press Enter to open, or pick "Create" to start a new note |
| Save a quick thought | Type in **Capture a thought** in the sidebar. It goes to today's daily note |
| Write or edit a note | **New note** or **Edit**. **Ctrl+S** saves. Link notes with `[[Note name]]` and tag with `#tag` |
| See what the AIs did | **AI activity**, filtered by AI |
| See how notes connect | **Graph**. Drag, scroll to zoom, click a dot to open |
| Manage to-dos | **Tasks**. Add tasks with a due date, or tick checkboxes right inside any note |
| Undo a change | Open the note, then **History**. Preview any version and click **Restore** |
| Rename or move a note | **Rename**. Every `[[link]]` to it is updated automatically |
| Find problems | **Health**. Gives a score plus broken links, duplicates, stale projects and overdue tasks |

Notes that AIs write while the app is open show up on their own within a few seconds. If an AI changes a note while you're editing it, Cortex warns you before saving, so nobody's changes are lost.

## Fewer tokens for every AI: chained recall

Without Cortex's recall tool, an AI searches, then reads three or four whole notes, and pays for every word. `brain_recall` does it in **one call**:

1. **Find** the best notes, by keyword and by meaning (common words like "how" and "should" are ignored, in English and Malay).
2. **Follow the chain of links.** The notes they link to, and that link to them, join in at a reduced weight. `hops=2` follows links of links. Connected context comes along even when it doesn't repeat your words.
3. **Score every passage.** Each note is split by heading, and each passage is scored on how well it matches. A heading that names the topic counts strongly.
4. **Pack the budget.** The best passages fill the token budget you choose: at most 3 per note, duplicates dropped, long paragraphs trimmed to the sentences that matter.

Measured on about 90 real documentation files:

| | Search + read top 3 notes | `brain_recall` (800-token budget) |
|---|---|---|
| Answer found (8 hand-written questions) | 5 / 8 | **8 / 8** |
| Answer found (30 random sections) | 20 / 30 | **26 / 30** |
| Tokens returned | 1,423,640 | **30,235 (98% fewer)** |

In a real Claude Code session asking the same question, recall gave the same answer with **41% fewer total input tokens and 3 turns instead of 5**. Claude Code's own fixed prompt is resent each turn, so fewer turns saves a lot. Recall takes about 25 ms on a normal vault and 65 ms on 100,000 notes, and repeat questions are cached until a note changes.

## Your full AI history, in one place

Cortex imports **every conversation you've had with your AI apps**: everything you typed and every reply, in full. Each conversation becomes one note under `Conversations/<App>/`. Both you and every connected AI can search them ("what did I decide about the logo last month?").

| App | How |
|---|---|
| Claude Code | Automatic (reads `~/.claude/projects`) |
| Codex CLI | Automatic (reads `~/.codex/sessions`) |
| Gemini CLI | Automatic (reads `~/.gemini/tmp`) |
| Cursor | Automatic, experimental (Cursor changes its storage format often) |
| ChatGPT (web) | Settings → Data controls → **Export data**, then in Cortex: **Conversations → Import from ChatGPT or Claude.ai** |
| Claude.ai (web) | Settings → Privacy → **Export data**, then import the same way |

- New conversations come in automatically every 5 minutes while the app is open, and whenever any AI starts. Unchanged sessions are skipped instantly. You can also run `python run.py import`.
- Tool output and hidden reasoning are dropped, and tool calls become one line ("used Bash: npm test"). That makes transcripts typically 10–150× smaller, while every word you and the AI wrote is kept.
- Obvious secrets (API keys, tokens, private keys, `password=...`) are replaced with `[REDACTED]` before saving.
- To turn the automatic import off, use `python run.py app --no-import`, or set `CORTEX_AUTO_IMPORT=off` for the AI apps.

## Free research and social media (no API keys)

The **Research** page, and every connected AI through `web_research` and `web_fetch`, can search:

**Web** (DuckDuckGo) · **News** (Google News) · **Wikipedia** · **Hacker News** · **Reddit** · **GitHub** · **Stack Overflow** · **arXiv** · **Bluesky** · **Mastodon** · **Lemmy** · **X/Twitter** · **YouTube**

It can also read any link as clean text: articles, **YouTube videos with full transcripts**, **X posts** with like and repost counts, **Reddit and Hacker News threads** with top comments, Bluesky posts and GitHub READMEs. Click **Save to brain** to keep one as a note with its source.

**Provider chains.** Each hard source has several independent free backends, tried in order. When one is rate-limited, blocked or changes its page, it's benched for 10 minutes and the next one answers. Results are cached (30 minutes for searches, 6 hours for pages), so repeat lookups cost nothing and stay polite.

| Source | Backends, in order |
|---|---|
| Web search | DuckDuckGo → DuckDuckGo Lite → Mojeek → Bing |
| **X / Twitter** search | Nitter mirrors (xcancel.com, nitter.poast.org, …) → web search for x.com posts |
| **X** post | FxTwitter → VxTwitter → X's official oEmbed |
| **X** account (`x.com/name`) | Recent posts through Nitter RSS mirrors |
| **YouTube** search | YouTube page → Invidious (4 instances) → Piped (3 instances) |
| **YouTube** transcript | YouTube captions → **yt-dlp** (if installed) → Invidious captions → Piped subtitles |
| **Reddit** search | Reddit JSON → old.reddit JSON → Reddit RSS → PullPush archive → web search for reddit.com |
| **Reddit** thread | Reddit JSON → old.reddit JSON → thread RSS → PullPush archive |

Tip: for the most reliable YouTube transcripts, install yt-dlp (`pip install yt-dlp`). Cortex finds it automatically.

Honest limits: these free mirrors come and go, which is why there are several of each. If every backend for a source is down, Cortex says so and carries on with the other sources. GitHub allows 60 searches an hour and Stack Exchange 300 a day without a key.

AI workflows: `/mcp__cortex__research <topic>` checks your brain first, then the web, and writes a cited note. `/mcp__cortex__pulse <topic>` reports what people are saying across X, Reddit, Hacker News, Bluesky, YouTube and news.

## Security

Cortex holds your whole AI history, so it's built to keep it on your computer and resist manipulation. Every item below is covered by an automated test that tries the attack.

| Threat | Protection |
|---|---|
| A website talking to the Cortex app (DNS rebinding, cross-site requests) | Only `127.0.0.1` / `localhost` Host headers are accepted. Cross-site browser requests and writes without the app's header are refused. A strict Content-Security-Policy is applied and framing is denied |
| Reading files outside the vault | Every path is checked, including history versions. Symlinks are never indexed. Reserved names (`.cortex`, `CON`, …) are blocked in any letter case |
| A poisoned web page making an AI rewrite the rules every AI follows | `_CORTEX.md`, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` and imported conversations can't be changed by an AI, only by you in the app. An AI can't pose as "you" |
| Prompt injection inside fetched pages | Web content is labelled as information, not instructions, both live and in saved notes (`trust: external`). Invisible and bidi characters and Unicode "tag" characters, used to hide instructions, are stripped |
| An AI fetching your router or intranet (SSRF) | Only public addresses are allowed. Each connection checks and connects to the same IP in one step (no DNS-rebinding gap). Redirects are re-checked, and links with embedded passwords are refused |
| Malicious responses | Size caps, a capped gzip decompressor (no "zip bombs") and request body limits |
| Clickable `javascript:` links | Only http(s) links are ever clickable or saved |
| Secrets in imported chats | API keys, tokens, private keys and passwords are redacted before saving |
| Mistakes | Every change is versioned (the last 30 per note), and deletes go to `.trash`. A corrupt index rebuilds itself from your notes |

## How much can it hold?

Tested on this machine with **100,000 notes (209 MB of text)**:

| | |
|---|---|
| Search | 13 ms |
| Save a note | 2 ms |
| Open a note (with backlinks) | under 1 ms |
| `brain_recall` (one call, linked notes included) | 65 ms |
| Search index on disk | 150 MB (smaller than the notes themselves) |
| Memory | about 185 MB |
| First full index (one time) | 45 s |

In practice, very heavy use (around 20 AI conversations a day) adds roughly 100 MB of text a year after import, so 100,000 notes is several years of everything. SQLite itself can hold terabytes. The practical limit is the background check for files edited outside Cortex, which grows with the number of files (about 2 s at 100,000). Cortex runs that check less often as the vault grows, and changes made through Cortex are always indexed instantly.

## Optional: search by meaning

1. Install [Ollama](https://ollama.com).
2. In a terminal, run `ollama pull nomic-embed-text`.
3. Restart Cortex. The sidebar footer shows **● Meaning search on**, and notes are processed in the background.

## What the AIs get

Every connected AI gets these tools:

| Tool | What it does |
|---|---|
| `brain_context` | Loads the vault rules, recent notes, open tasks and what the other AIs did, in one call (with a token budget) |
| **`brain_recall`** | **The main way to pull context.** One call returns only the passages that answer the question, from the best notes, the notes linked to them and your past AI conversations, packed into a token budget |
| `brain_batch` | Several writes in one call (decisions, tasks and a log entry together) |
| `brain_search` | Searches by keyword and by meaning. Returns short snippets, which saves tokens |
| `brain_read` | Reads a note by name, or just one section, or a capped length. Also lists links, backlinks and related notes |
| `brain_write` | Creates or appends to a note. Warns when a similar note already exists |
| `brain_edit` | Find-and-replace, or rewrite one section, without resending the whole note |
| `brain_move` | Renames a note and fixes every link to it |
| `brain_tasks` | Lists, adds and completes tasks |
| `brain_capture` | Saves a fact or decision to today's note |
| `brain_log` / `brain_activity` | Writes to and reads the shared logbook (work and decisions) |
| `brain_recap` | Gathers what changed over the last N days, for reviews |
| `brain_health` | Runs the vault health check |
| `web_research` | Free search across 13 web and social sources |
| `web_fetch` | Reads any link (article, YouTube transcript, X post, thread) and can save it |

**Built-in workflows.** Claude Code shows these as slash commands, and other AI apps show them as prompts:

| Command | What it does |
|---|---|
| `/mcp__cortex__save` | Saves everything worth keeping from the conversation into the right notes |
| `/mcp__cortex__decide` | Records a decision with its reasons, and flags any earlier decision it conflicts with |
| `/mcp__cortex__person` · `/mcp__cortex__project` | Creates or updates a person or project note |
| `/mcp__cortex__review` | Writes a daily or weekly review note |
| `/mcp__cortex__tidy` | Runs the health check and fixes what you approve |
| `/mcp__cortex__challenge` | Tests an idea against everything the brain already knows |
| `/mcp__cortex__research` | Researches a topic (brain first, then the free web) and saves it with citations |
| `/mcp__cortex__pulse` | Reports what people are saying about a topic across social media |

The rules the AIs follow are in `_CORTEX.md` inside your vault. Edit that note to change how they behave. AIs can't delete notes. Only you can, from the app, and deleted notes go to `.trash` so you can recover them.

## Files

```
your vault/
  _CORTEX.md            rules for the AIs (edit freely)
  Home.md, Inbox/, Daily/, Projects/, Areas/, People/, Resources/, Archive/
  AGENTS.md, CLAUDE.md, GEMINI.md   short pointers each AI app reads on its own
  .cortex/activity/     the shared logbook, one file per computer
  .cortex/history/      earlier versions of every changed note

Kept outside the brain folder, on each computer (never synced):
  Windows %LOCALAPPDATA%\Cortex\vaults\...   macOS ~/Library/Application Support/Cortex/vaults/...
  Linux ~/.local/share/cortex/vaults/...      search index + import state (safe to delete; rebuilds)
  .trash/               deleted notes (recoverable)
```

`connect` backs up every config file it changes (`*.bak-<date>`) and leaves your other settings alone. Running it again is safe.

## Works with obsidian-second-brain

Cortex reuses the main ideas of [eugeniughelbur/obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain) (MIT): an Obsidian vault as AI memory, one logbook shared by every agent ("Crew"), and AI-first note rules. Cortex adds an app on top of those ideas, plus a single fast engine every AI shares. To get that project's 49 Claude Code slash commands (research, self-rewriting notes, scheduled agents), install its plugin and point it at the **same vault**. The two work side by side:

```
/plugin marketplace add eugeniughelbur/obsidian-second-brain
/plugin install obsidian-second-brain@obsidian-second-brain
```

## Commands

```
python run.py init [VAULT]    create or adopt a vault
python run.py app [--port N]  open the app
python run.py connect         connect AI apps (--all to write configs even if an app isn't detected)
python run.py index           rebuild the search index
python run.py import [FILE]   import AI conversations now (or a ChatGPT/Claude.ai export file)
python run.py cloud           keep the brain in Google Drive too (copy, verify, switch, back up)
python run.py backup [--to DIR] [--full]   verified snapshot now (--full adds version history)
python run.py restore ZIP DIR restore a snapshot into a new folder
python run.py mcp             the MCP server (AI apps start this themselves)
python -m unittest discover tests
```
