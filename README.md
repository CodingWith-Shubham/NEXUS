# JARVIS

A Pipecat AI voice agent built with a realtime speech-to-speech pipeline.

## Configuration

- **Bot Type**: Web
- **Transport(s)**: SmallWebRTC
- **Pipeline**: Realtime
  - **Speech-to-speech**: Google Gemini Live (hears, thinks and speaks)

## How JARVIS is put together

- **Voice front** (`server/bot.py`): Gemini Live with a few fast tools plus `delegate_task`.
- **Background worker** (`server/worker.py`): runs in its own process. It takes tasks from the
  Supabase `tasks` table, writes results back, and the voice front announces them, either during
  the call or at the start of the next one. Its text models are listed in `[worker] models` in
  `jarvis.toml` and tried in order (`server/jarvis/llm.py`, any OpenAI-compatible endpoint). The
  default is NVIDIA Nemotron via OpenRouter (`NEMOTRON_KEY`). The same models also write session
  summaries and web-search answers.
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
- **Calendar and email** (`server/skills/workspace/`, Phase 3): one provider interface
  (`server/jarvis/workspace/`), chosen with `[workspace] provider`. Google (Gmail + Calendar) is
  implemented; Microsoft Graph is a stub. Each user connects their own Google account on
  `/settings` (separate from signing in). Tokens are encrypted with `JARVIS_TOKEN_KEY`, stored in
  `oauth_tokens` under that user, and refreshed automatically. The voice gets `list_events` and `search_email`; everything else
  (`find_free_time`, `create_meeting`, `update_meeting`, `cancel_meeting`, `read_email`,
  `draft_email`, `send_email`, `reply_to_email`) runs in the worker through `delegate_task`.
  Sending, replying, and meeting changes that involve other people need approval. Email content
  is marked untrusted and is never followed as instructions.
- **Deep research** (`server/skills/research/`, Phase 4): `start_research(topic, depth)`
  queues a worker job that plans 3-6 sub-questions, searches each with the `[search]`
  provider, reads the best pages in full (skipping failed and paywalled ones, and never
  fetching private or internal addresses), extracts findings that must quote the page they
  came from, and writes a cited report: summary, a section per sub-question, disagreements,
  "what I couldn't confirm" and a source list. Reports are saved per user in `reports` and
  shown at `/reports`; JARVIS announces each one with a short spoken summary and the browser
  shows a link. Voice also gets `get_research_status`, `cancel_research` and `ask_report`
  (answers from a saved report without searching again). Limits per depth (searches, pages,
  time) are in `[research.*]` in `jarvis.toml`; one job per user at a time. If search or the
  worker model is down the job fails with a clear message instead of writing from memory.
  `uv run python scripts/research.py you@example.com "topic" --depth quick` runs one live job
  without the worker.
- **Sales** (`server/skills/sales/`, Phase 5): each user's sales profile (company, target
  industries and regions, buying signals with points, score thresholds, job titles, email tone)
  is edited on `/sales/profile` or seeded with `scripts/seed_sales_profile.py`; no company
  knowledge is in code. `find_leads` runs a worker job on the same search-and-read flow as deep
  research (`server/jarvis/reading/`): every signal is checked against the fetched page (excerpt,
  company name, a date within six months), leads are scored from the profile, de-duplicated by
  normalised name and domain, and shown on `/leads` with their evidence. Contacts come from the
  provider in `[contacts]` (SignalHire; Lusha is a stub): each lookup needs the user's approval,
  only runs for approved leads, and its credits are tracked per user. Without a public
  `callback_base_url` SignalHire is asked synchronously; with one, results arrive at
  `/webhooks/contacts/signalhire/...` (signed URL, matched by request id). `draft_outreach` writes
  an email under 120 words that opens with the verified signal and its source, plus a LinkedIn
  note to copy; sending goes through `send_email` and the approval gate and marks the lead
  contacted. Scheduled sweeps (`scheduled_jobs`, run by the worker's scheduler) only find and
  save leads; they're announced at the next session.
- **Marketing** (`server/skills/marketing/`, Phase 6): each user connects LinkedIn on `/settings`
  (OAuth with `openid profile email w_member_social`; a connection, not a sign-in), and keeps a
  marketing profile (voice, topics, audience, never-say list, hashtags, 3-5 sample posts) on
  `/marketing/profile`. `draft_post` writes in their voice from a topic, a research report or a
  lead's verified news; figures not in the source are removed and a source line is added when
  needed. `create_post`, `comment_on_post`, `react_to_post` and `schedule_post` all go through
  the approval gate with the exact final text; a scheduled post is published by the worker's
  scheduler only if its text still matches what was approved. Comments and reactions become
  draft-only if LinkedIn refuses them for the app (checked when connecting, or with
  `scripts/linkedin_probe.py`). Every LinkedIn action is in `audit_log` with LinkedIn's response.
  Drafts, scheduled and published posts are on `/content`. Uses LinkedIn's official versioned API
  only (`[linkedin]` in `jarvis.toml`); nothing reads the feed or other people's posts.
- **Accounts** (`server/jarvis/web/`, `server/jarvis/accounts.py`): invite-only sign-in with
  Google (openid, email, profile) on the bot's own server. `ALLOWED_EMAILS` is the invite list.
  The session is a signed, HttpOnly, SameSite=Lax cookie (Secure when `[web] public_url` is
  https) that lasts `[web] session_hours`. A gate in front of every route, including Pipecat's
  `/start`, `/api/offer` and `/client`, sends signed-out visitors to `/login` (pages) or answers
  401 (APIs). The user who starts a voice session is taken from the cookie, never from the
  request body, and every tool reads it from the session (`ctx.user_id`); no tool takes a
  `user_id` argument. The store is bound to one user and filters every query by `user_id`; the
  worker runs each task as its owner.
- **Database**: the migrations in `supabase/migrations/` (`0001_core.sql`, `0002_assistant.sql`,
  `0003_multi_user.sql`, `0004_research.sql`, `0005_sales.sql`, then `0006_marketing.sql`) create the tables. Run each once, in order, in the Supabase SQL
  editor. Then `uv run python scripts/check_store.py you@example.com` checks every store
  operation against Supabase as that user, and that another user can't see the rows.

Run JARVIS in two terminals from `server/`: `uv run bot.py` and `uv run worker.py`, then open
`http://localhost:7860` (use `localhost`, not `127.0.0.1`) and sign in.
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
   # Edit .env and add your API keys, ALLOWED_EMAILS and JARVIS_SESSION_SECRET
   ```

   On the Google OAuth client (Google Cloud Console > Credentials), add both authorized
   redirect URIs: `http://localhost:7860/auth/login/callback` (sign-in) and
   `http://localhost:7860/auth/google/callback` (Gmail + Calendar).

4. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

## Testing with evals

This project includes behavioral evals: scripted conversations that drive the bot headless — no live call needed. Starter scenarios live in `server/evals/`; edit them as your bot takes shape and copy them to add more.

The eval harness can't sign in, so the `/ws` route it uses is closed by default. To run evals,
set `JARVIS_EVAL_USER_EMAIL` in `server/.env` to a JARVIS user (one who has signed in once);
eval sessions then run as that user, and only from this machine. Leave it empty otherwise.

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