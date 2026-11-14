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
- **Database**: `supabase/migrations/0001_core.sql` creates the tables `memory`, `tasks`,
  `pending_actions`, `audit_log` and `oauth_tokens`. Run it once in the Supabase SQL editor.

Run JARVIS in two terminals from `server/`: `uv run bot.py` and `uv run worker.py`.
Unit tests: `uv run pytest`. Spoken Phase 1 check: `uv run pipecat eval run evals/phase1_audio.yaml -v`.

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

## Project Structure

```
JARVIS/
├── server/              # Python bot server
│   ├── bot.py           # Voice front (Gemini Live)
│   ├── worker.py        # Background task worker
│   ├── jarvis.toml      # Enabled skill packs, worker model, limits
│   ├── jarvis/          # Core: store, registry, approval gate, tasks, announcer
│   ├── skills/          # Skill packs (demo for now)
│   ├── tests/           # Core unit tests
│   ├── evals/           # Behavioral eval scenarios
│   ├── pyproject.toml   # Python dependencies
│   ├── .env.example     # Environment variables template
│   ├── .env             # Your API keys (git-ignored)
│   ├── Dockerfile       # Container image for Pipecat Cloud
│   └── pcc-deploy.toml  # Pipecat Cloud deployment config
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