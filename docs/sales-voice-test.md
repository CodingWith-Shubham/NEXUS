# Sales pack: voice test

A live test of Phase 5, about 20 minutes. It uses real web searches and worker-model calls
(a lead search takes about 6 searches and 1 to 3 model calls). Only step 6 spends SignalHire
credits: one credit per matched person.

## Before you start

1. Run `supabase/migrations/0005_sales.sql` in the Supabase SQL editor.
2. Seed your profile. Fill in `server/skills/sales/tasc_profile.toml`, then from `server/`:
   `uv run python scripts/seed_sales_profile.py mamgai75@gmail.com skills/sales/tasc_profile.toml`
3. Restart `uv run bot.py` **and** `uv run worker.py`. The worker runs lead searches and the
   scheduler, so it must be running the new code.
4. **Use headphones**, sign in at `http://localhost:7860`, and connect the voice client.
5. Open `http://localhost:7860/sales/profile` and check your profile is there.

## 1. Find leads

Say: **"Find me some new leads."**

- JARVIS says it's looking and will tell you what it finds. The worker logs a `lead_search`
  task.
- A few minutes later JARVIS says something like "I found 3 new leads: X (60), Y (45)...". If
  nothing clears your minimum score, it says so; that's a valid result, not a failure.
- On `/leads`, each lead has a score, a signal, a clickable source and status "new". On a lead's
  page, every piece of evidence shows a verbatim excerpt, its source link and a publication date
  within the last six months.

Say: **"Find leads in <one of your industries> that are expanding."** The search should focus
on that industry.

## 2. List, approve, reject

- **"What are my best leads?"** JARVIS reads company, score and the reason. It doesn't read IDs
  or web addresses.
- **"Approve <company A>."** **"Reject <company B>, too small."**
- `/leads` shows the new statuses, and each lead's History lists the change.

## 3. Duplicates

Say **"Find me some new leads"** again. Companies already in your leads aren't added a second
time; JARVIS may say "N more were already in your leads".

## 4. Draft outreach (no email address yet)

Say: **"Draft an outreach email for <company A>."**

- JARVIS reads the gist and says the draft is on the lead's page.
- On the page: the email's first sentence names the news and where it was reported, the word
  count is under 120, and the tone matches your profile. There's also a LinkedIn note with a
  Copy button. Nothing is sent: there's no email address yet.

## 5. Contact lookups need approval (no credits spent here)

- On a lead that's still "new", the page says to approve it first; there's no lookup button.
- Say **"Look up the contact for <company B>"** (the rejected one). JARVIS delegates it, and
  the worker refuses because the lead isn't approved. No approval is asked and nothing is spent.

## 6. Look up a contact (spends 1 SignalHire credit if a person matches)

On **<company A>**'s page (approved), press **Find contact**.

- An approval box says it will use 1 credit if a person is matched, and how many you've used.
  Nothing has happened yet.
- Press **Yes, go ahead**, or say "confirm".
  - With no `callback_base_url` (the default), the result appears straight away: a person,
    their title, emails and phones, or "not found" with no credit used.
  - The page shows "1 credit used by you" and the account balance SignalHire reported.

To test the callback mode instead: run ngrok (`ngrok http 7860`), set
`[contacts] callback_base_url = "https://<your-ngrok>.ngrok-free.app"` in `jarvis.toml`, and
restart both processes. The lookup then shows "pending" until SignalHire calls back, usually
within seconds, sometimes a minute. Watch the bot log for `/webhooks/contacts/signalhire/...`
returning 200.

## 7. Send outreach

Once <company A> has a contact email, say: **"Draft an outreach email for <company A>."**

- JARVIS reads the gist and asks whether to send it. Say **"Yes, send it."**
- The email arrives at the contact's address (test with a lead whose contact is you, or a
  colleague who knows). The lead becomes "contacted", and History shows "email sent".
- Saying "no" instead leaves it unsent and the lead stays "approved".

## 8. Schedules

- **"Find leads every weekday at 8am, one industry per day."** JARVIS confirms "every weekday
  at 8:00 AM, one industry per run".
- **"What lead schedules do I have?"** It lists them, with the next run.
- **"Pause the first schedule."**, then **"Resume it."**
- To see a run without waiting until 8am, make one fire now by setting its next run in Supabase:

  ```sql
  update scheduled_jobs set next_run_at = now() where status = 'active';
  ```

  Within a few seconds the worker logs "Schedule ... queued task ...", then the lead search.
  It doesn't speak. Disconnect and reconnect, and JARVIS mentions the sweep's new leads at
  the start. They're on `/leads` marked "found by a scheduled sweep".
- A sweep never looks up contacts or sends anything. Check that no new rows appear in
  `contact_lookups` and there are no pending approvals.
- Optional digest: **"Find leads every Monday at 9am and email me a digest."** When it runs, the
  digest goes only to your own connected Gmail address.
- **"Delete the lead schedules."** (by number)

## 9. Privacy

Sign in as the second account in a private window. `/leads` is empty, and opening one of your
lead URLs shows "Lead not found". Saying "What are my best leads?" there gets none of yours.

## Cost check

```sql
select status, count(*), sum(credits_used) from contact_lookups group by status;
```

Credits are only spent on rows with status `success`, one each.
