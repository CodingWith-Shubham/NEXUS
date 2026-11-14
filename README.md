# JARVIS

A Pipecat AI voice agent built with a realtime speech-to-speech pipeline.

## Configuration

- **Bot Type**: Web
- **Transport(s)**: SmallWebRTC
- **Pipeline**: Realtime
  - **Speech-to-speech**: Google Gemini Live (hears, thinks and speaks)

## How JARVIS is put together

- **Voice front** (`server/bot.py`): Gemini Live with a few fast tools plus `delegate_task`.
- **Background worker** (`server/worker.py`): a Gemini text model in its own process. It takes
  tasks from the Supabase `tasks` table, writes results back, and the voice front announces them,
  either during the call or at the start of the next one.
- **Core** (`server/jarvis/`): storage, tool registry, skill loader, approval gate
  (`confirm_action` / `cancel_action`), `delegate_task` and `get_task_status`.
- **Skill packs** (`server/skills/<name>/`): each has its own tools, prompt sections and tests.
  They are listed in `server/jarvis.toml`; remove a name to unload that pack.
- **General assistant** (`server/skills/assistant/`, Phase 2): `remember` / `recall` (long-term
  memory in four categories), `add_note` / `list_notes`, and `add_reminder` / `list_reminders`.
  Reminders are stored and listed; nothing delivers them yet.
- **Session continuity** (`server/jarvis/sessions.py`): each session starts with the current
  date and time, the last few conversation summaries and the most relevant memories. When it
  ends, a short summary is saved.
- **Web search** (`server/jarvis/search.py`, `server/jarvis/search_providers.py`): set
  `[voice] web_search` in `jarvis.toml`. `"live"` turns on Google Search grounding inside Gemini
  Live, which needs a billed Gemini key. `"tool"` adds a `web_search` tool backed by
  `[search] provider`: `"tavily"` (needs `TAVILY_API_KEY`; the worker model writes the spoken
  answer from the results) or `"google"`. Source URLs go to `audit_log` and to the browser as a
  `search-sources` message. If search fails, JARVIS says it's unavailable and doesn't answer from
  memory. To add Exa or Serper, write a provider class and register it in `PROVIDERS`.
  `uv run python scripts/search.py "question"` runs one live search.
- **Laptop access** (`server/skills/laptop/`, `laptop_agent/`): find, read, move and trash files
  in `~/Downloads` on your Mac, and nothing else. See [Laptop access](#laptop-access-macos-downloads-only).
- **Database**: the migrations in `supabase/migrations/` (`0001_core.sql`, `0002_assistant.sql`,
  then `0003_laptop.sql`) create the tables. Run each once, in order, in the Supabase SQL editor.
  Then `uv run python scripts/check_store.py` checks every store operation against Supabase.

Run JARVIS in two terminals from `server/`: `uv run bot.py` and `uv run worker.py`.
Unit tests: `uv run pytest`. Spoken checks: `evals/phase2_audio.yaml`, then
`evals/phase2_continuity_audio.yaml` (run with `uv run pipecat eval run <file> -v`).

## Setup

### Server

1. **Navigate to server directory**:

   ```bash
   cd server
   ```

2. **Install dependencies**:

   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

4. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

## Testing with evals

This project includes behavioral evals: scripted conversations that drive the bot headless — no live call needed. Starter scenarios live in `server/evals/`; edit them as your bot takes shape and copy them to add more.

From `server/`, run the bot, then drive scenarios against it from a second terminal (the bot stays up across runs):

```bash
uv run bot.py
# In another terminal:
uv run pipecat eval run evals/starter_audio.yaml -v   # full audio round trip (speech-to-speech bots are tested in audio mode)
```

`eval:` criteria are scored by a judge LLM — a local Ollama by default (`ollama pull gemma4:12b`). The comments in the scenario files cover the schema and how to use an OpenAI judge instead.

## Laptop access (macOS, Downloads only)

JARVIS can find, read, organize and trash files in `~/Downloads` on your Mac, and nothing else.
The voice front never touches the laptop: it writes signed commands to Supabase, and a small
agent on the Mac (`laptop_agent/`) polls for them, checks them and runs them.

```
voice (bot.py) ──insert signed command──▶ Supabase laptop_commands ◀──poll / claim / finish── laptop_agent (your Mac)
                ◀──read result──────────                            ◀──heartbeat every 15 s──
```

### Tools

| Voice tool | Agent tool | Approval | What it does |
|---|---|---|---|
| `laptop_find` (action `list`) | `list_dir` | no | names, sizes and dates in a folder |
| `laptop_find` (action `search`) | `search_files` | no | file-name search, capped results |
| `laptop_find` (action `read`) | `read_file` | no | txt, md, csv, json, log; size-capped and truncated |
| `laptop_move_file` | `move_file` | **yes** | move or rename inside Downloads; never overwrites |
| `laptop_delete_file` | `delete_file` | **yes** | moves the file to `~/Downloads/.jarvis_trash/` (timestamped); never a permanent delete |
| `laptop_pause` | `pause` | no | creates `laptop_agent/PAUSE`; resuming is only possible on the laptop |

The list, search and read actions share one voice tool to keep Gemini Live's tool list short.
There is deliberately no tool to write files, run scripts or programs, open apps or URLs, or read
system information. PDF reading is not included (no PDF library is a dependency).

### Setup

1. **Database**: run `supabase/migrations/0003_laptop.sql` in the Supabase SQL editor.
2. **Secrets**: generate two values, for example with
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`:
   - `LAPTOP_AGENT_SECRET`: signs commands. Put the same value in `server/.env` and `laptop_agent/.env`.
   - `LAPTOP_AGENT_DB_TOKEN`: lets the agent call its database functions. Laptop only.
3. **Agent environment**: `cp laptop_agent/.env.example laptop_agent/.env` and fill in
   `SUPABASE_URL`, `SUPABASE_ANON_KEY` (the anon / publishable key, **not** service_role),
   `LAPTOP_AGENT_SECRET` and `LAPTOP_AGENT_DB_TOKEN`.
4. **Register the token**: `cd laptop_agent && uv sync && uv run python -m laptop_agent hash-token`
   prints an `insert` statement holding only the token's SHA-256. Run it in the SQL editor.
5. **Config**: `cp config.example.toml config.toml` (in `laptop_agent/`). It starts with
   `read_only = true`.
6. **Check**: from `server/`, `uv run python scripts/check_store.py`. With `SUPABASE_ANON_KEY`
   in `server/.env` it also checks that the anon key can't read the commands table or claim one
   without the token.

### Running the agent

```bash
cd laptop_agent
uv run python -m laptop_agent
```

It runs in the foreground (auto-start comes later). It opens no ports: it only makes outbound
HTTPS calls to Supabase. Tests: `cd laptop_agent && uv run pytest`.

### Granting only the Downloads permission

macOS asks for folder permissions per app, and the app is the one that starts Python: your
terminal (Terminal, iTerm or VS Code), not `python` itself.

1. The first time the agent lists Downloads, macOS asks whether your terminal may access files in
   your Downloads folder. Click **Allow**. Or go to **System Settings → Privacy & Security → Files
   and Folders → your terminal** and turn on **Downloads Folder** only.
2. Do **not** grant Full Disk Access. Check **System Settings → Privacy & Security → Full Disk
   Access**: if your terminal is listed and on, turn it off, or run the agent from a terminal app
   that isn't listed.
3. If macOS later asks about Documents or Desktop for that terminal, click **Don't Allow**.

**Check that other folders are blocked.** Run these from the terminal that runs the agent:

```bash
ls ~/Documents   # should fail: "Operation not permitted" (blocked by macOS)
ls ~/Desktop     # should fail: "Operation not permitted" (blocked by macOS)
```

`~/.ssh` is **not** protected by macOS: any program you run can read it. So is any folder, if
the terminal has Full Disk Access. What blocks these is the agent's own sandbox. Check it from
`laptop_agent/`:

```bash
uv run python -c "
from laptop_agent.config import load_config
from laptop_agent.sandbox import Sandbox, SandboxError
sb = Sandbox(load_config())
for p in ['~/Documents', '~/Desktop', '~/.ssh/id_rsa', '../Documents', '/etc/passwd']:
    try:
        sb.resolve(p); print('ALLOWED (this is a bug):', p)
    except SandboxError as e:
        print('refused:', p, '->', e.code)
"
```

Every line should say `refused`. `uv run pytest` covers the same cases, plus symlinks.

### Security model

- **Sandbox**: only `~/Downloads` is accessible. `allowed_roots` can name folders inside it
  (narrowing); anything else stops the agent at startup. Every path is resolved with `realpath`
  before it is checked, so `..`, symlinks and aliases pointing elsewhere are refused. Any symlink
  in a path is refused, and search and listing skip symlinks. Move and delete work on regular
  files only. For a move, both source and destination must be inside.
- **Protected names**: hidden files, and names matching `*.pem`, `*.key`, `*.env`, `*secret*`,
  `*token*`, `*password*`, `*passwd*`, `*.kdbx`, `id_rsa*`, `*seed*`, `*wallet*`, `*.p12`,
  `*.pfx`, `*.mobileconfig` or `*.keychain*` (any case), are never shown in a list or search and
  can't be read, moved or deleted. The config can add patterns but not remove these.
- **Trash**: `.jarvis_trash` shows by name in a listing of Downloads, but nothing inside it can
  be listed, read or moved by JARVIS. Restore files from it in Finder.
- **Signed commands**: the server signs every command with HMAC-SHA256 over (id, tool, args,
  expires_at), using `LAPTOP_AGENT_SECRET`. The agent rejects:
  - commands that are unsigned or tampered with;
  - commands that have expired (120 s TTL) or carry an expiry further away than its own TTL;
  - replays.

  A command can be claimed only once (a `for update skip locked` claim). The agent takes each
  tool's tier from its own tool table, never from the row.
- **Approval**: moves and deletes always go through `confirm_action` / `cancel_action`. The
  command is created, signed and marked `approved` only after you say yes, and the confirmation
  names the exact file and destination. Before asking, JARVIS checks the heartbeat: if the laptop
  is offline, paused or read-only, it says so instead of asking.
- **Modes**: `read_only = true` (the default) makes the agent reject moves and deletes even when
  approved. `dry_run = true` makes them only report what they would do.
- **Kill switch**: say "pause laptop access" (`laptop_pause`), or `touch laptop_agent/PAUSE`, or
  set `[agent] enabled = false`. While paused the agent claims nothing. Resume by deleting
  `laptop_agent/PAUSE` on the laptop; there is no voice command for it.
- **Limits**: one command at a time, at most 20 per minute, a 20 s timeout per command, and capped
  read sizes, result counts and output size.
- **Prompt injection**: file names, file contents and search results come back to the model as
  `untrusted_data`. The laptop prompt section tells it that nothing written in them is a request
  from you. The laptop tools are offered to the voice model only, never to the background worker.
- **No code execution**: the agent uses the standard library for file operations. It has no
  `subprocess`, `os.system`, `eval` or `exec`, and never opens or runs a file. A test scans the
  package for these.
- **Audit**: every command goes to `audit_log`:
  - **requested:** `laptop_command_requested`, written by the server;
  - **asked to approve, refused before asking, approved, cancelled:** the existing `action_*`
    events;
  - **outcome:** `laptop_command_done` / `_rejected` / `_failed` / `_expired`, written in the
    same transaction that records it.

  Entries hold the tool, the paths as given, the status and sizes. File contents are never logged.
- **Keys and RLS**:
  - **Server:** uses the service_role key, as before.
  - **Agent:** uses the anon key. The laptop tables have RLS on and no policies, so the anon key
    can't read or write them directly. It can only call `laptop_claim_command`,
    `laptop_finish_command` and `laptop_heartbeat_ping`, and each of those checks the agent
    token against the stored SHA-256.
  - **Recommendation:** keep RLS on for every table and add no anon policies, so only the server
    (service_role) and the agent (these three functions) can touch the laptop tables.
  - **Secrets:** stay in `.env` files and are never written to Supabase or to logs.

### Rollout

1. `read_only = true`: test search, read and list by voice.
2. `dry_run = true` and `read_only = false`: check the move and delete confirmations and the
   `audit_log` entries. Run `server/evals/laptop_audio.yaml` here; its header lists the test files
   to create. Run `evals/laptop_offline_audio.yaml` with the agent stopped.
3. `dry_run = false`: real operations, on a test folder inside Downloads first.

## Project Structure

```
JARVIS/
├── server/              # Python bot server
│   ├── bot.py           # Voice front (Gemini Live)
│   ├── worker.py        # Background task worker
│   ├── jarvis.toml      # Enabled skill packs, worker model, limits
│   ├── jarvis/          # Core: store, registry, approval gate, tasks, announcer
│   ├── skills/          # Skill packs (assistant, laptop, demo)
│   ├── tests/           # Core unit tests
│   ├── evals/           # Behavioral eval scenarios
│   ├── pyproject.toml   # Python dependencies
│   ├── .env.example     # Environment variables template
│   ├── .env             # Your API keys (git-ignored)
│   ├── Dockerfile       # Container image for Pipecat Cloud
│   └── pcc-deploy.toml  # Pipecat Cloud deployment config
├── laptop_agent/        # Runs on your Mac: signed, sandboxed file commands in ~/Downloads
│   ├── laptop_agent/    # The agent package (config, sandbox, signing, tools, runner)
│   ├── tests/           # Sandbox, signing, modes and limits tests
│   └── config.example.toml
├── supabase/migrations/ # Database schema
├── .gitignore           # Git ignore patterns
└── README.md            # This file
```

## Deploying to Pipecat Cloud

This project is configured for deployment to Pipecat Cloud. You can learn how to deploy to Pipecat Cloud in the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2-deploy-to-production).

Refer to the [Pipecat Cloud Documentation](https://docs.pipecat.ai/pipecat-cloud/introduction) to learn more about configuring, deploying, and managing your agents in Pipecat Cloud.

## Building with an AI coding agent

Extending this bot with Claude Code, Codex, or another AI coding assistant? Give it live, accurate Pipecat context instead of stale training data with the **Pipecat Context Hub** — a local index of Pipecat docs, examples, and API source your agent queries over MCP:

```bash
# The Context Hub ships with the CLI
uv tool install "pipecat-ai[cli]"
pipecat context-hub install
```

`install` registers the MCP server with each coding agent it finds and builds the index — a few minutes and about 900 MB the first time. MCP servers load at session start, so do this before opening your coding session, and note the server won't start against an empty index. See the [Pipecat Context Hub docs](https://docs.pipecat.ai/api-reference/context-hub) for the full setup.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)