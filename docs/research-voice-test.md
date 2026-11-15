# Deep research: voice test

A live test of Phase 4 by voice, about 15 minutes. It uses real web searches and worker-model
calls: a quick job takes about 3 searches and 4 to 6 model calls. OpenRouter's free models
allow 50 calls a day in total, so leave room for retries.

## Before you start

1. Run `supabase/migrations/0004_research.sql` in the Supabase SQL editor.
2. From `server/`: `uv run python scripts/check_store.py mamgai75@gmail.com`. It should end
   with "All good." (it now checks reports too).
3. Restart both processes so they load the research pack: `uv run bot.py` and
   `uv run worker.py`.
4. **Use headphones.** With speakers, JARVIS hears itself and can start research you never
   asked for.
5. Open `http://localhost:7860`, sign in, and press Connect in the voice client. Keep the
   worker terminal visible.

## 1. Start a quick report

Say: **"Do some quick research on the electric scooter market in India."**

- JARVIS says it has started and will tell you when it's done, in a few minutes. It doesn't
  read out any results yet.
- The worker terminal logs the task "for user <your id>" and then the research steps.
- In a second tab, `/reports` lists it as running with a percentage. Its page shows progress
  and refreshes itself.

## 2. Ask how it's going

Say: **"How's the research going?"**

- JARVIS gives the current step or percentage from `get_research_status`. It shouldn't guess.

## 3. One job at a time

While it's still running, say: **"Also research coffee prices in India."**

- JARVIS says a research job is already running and offers to cancel it or wait. Nothing new
  appears on `/reports`.

## 4. The announcement

Keep talking about something else, or stay quiet, until it finishes (usually 2 to 4 minutes).

- JARVIS interrupts the silence with two or three sentences of summary and says the full
  report is on the Reports page.
- A box in the corner of the voice page says "Report ready: ..." with an Open link.
- Open the report and check:
  - Every summary and section line ends with citation numbers. Clicking one jumps to its
    source, and each source link opens the real page in a new tab.
  - Sources show their publication date where the page gave one.
  - "What I couldn't confirm" lists gaps, and any limit that stopped the job early.
  - "Other pages" (at the bottom) lists pages that couldn't be read and why.

## 5. Follow-up questions (no new search)

Say: **"What did the report say about charging stations?"**

- You get an answer drawn from the report. The worker terminal shows no new search.

Say: **"What did it say about Tesla's share price?"**

- JARVIS says the report doesn't cover that and offers to research it. It doesn't answer from
  memory.

## 6. Cancel mid-run

Say: **"Research India's semiconductor policy, standard depth."** Wait about 20 seconds,
then say: **"Cancel that research."**

- JARVIS confirms it's cancelled. Within a few seconds the worker logs "cancelled", and
  `/reports` shows it as cancelled with no report body. Nothing is announced afterwards.

## 7. Email me this report

Google must be connected in Settings for this step.

1. On the finished report's page, press **Email me this report**.
   - A box appears: "Approve this email?" with the recipient (your sign-in address) and a
     preview. Nothing is sent yet.
2. Say **"confirm"** to JARVIS, or press **Send it**.
   - The page says it was emailed, and the email with the full report and a link to the page
     arrives in your inbox.
3. Optional: press Email again, then **Cancel**. Nothing is sent.

## 8. Privacy check

Sign in as the second account in a private window.

- Paste the first account's report URL. You should see "Report not found".
- `/reports` is empty for this account.
- Say "What did my last report say?" JARVIS says there's no finished report.

## 9. Optional: failure modes

- **Search down:** set `TAVILY_API_KEY` to `bad` in `server/.env`, restart the worker, and
  start a quick job. JARVIS announces that the research didn't finish because web search is
  unavailable, and there's no report body. Put the key back afterwards.
- **Model down:** if the free models have run out for the day, the job fails with "the worker
  model is unavailable" instead of writing a report.

## Without voice

To run one job and see the full output in the terminal:

```bash
cd server
uv run python scripts/research.py mamgai75@gmail.com "Electric scooter market in India" --depth quick
```

`evals/research_audio.yaml` checks the start, status and cancel tool calls headlessly. It
needs `JARVIS_EVAL_USER_EMAIL` set.
