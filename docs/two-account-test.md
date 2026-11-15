# Two-account test

A manual check that two JARVIS users can't see each other's data, by voice or over HTTP.
Takes about 20 minutes. **A** is `mamgai75@gmail.com` (the first user, who owns all data from
before accounts existed). **B** is `mukulkaushik457@gmail.com`.

Use two separate browser sessions so the cookies don't mix: a normal window for A and a
private/incognito window (or another browser) for B. Always use `http://localhost:7860`, not
`127.0.0.1`.

## 0. Before you start

1. In Google Cloud Console, OAuth consent screen > Test users: A, B and
   `krishanbagoria@gmail.com` are listed (while the app is in Testing mode).
2. Run `supabase/migrations/0003_multi_user.sql` in the Supabase SQL editor. Check the email
   on the line marked `>>> The first user` first.
3. Check that `server/.env` has `ALLOWED_EMAILS` (all three addresses) and a
   `JARVIS_SESSION_SECRET`.
4. For this test only, add `"demo"` to `[skills] enabled` in `server/jarvis.toml`. It
   provides `send_test_message`, a harmless action that waits for approval. Remove it again
   afterwards.
5. From `server/`: `uv run pytest` (all pass), then start `uv run bot.py` and
   `uv run worker.py` in two terminals.

## 1. Signed-out visitors get nothing

In a fresh private window, or with curl from `server/`:

```bash
curl -i http://localhost:7860/                   # 302 -> /login (a browser sees the login page)
curl -i -X POST http://localhost:7860/start -H "Origin: http://localhost:7860" -d '{}'   # 401
curl -i -X POST http://localhost:7860/api/offer -H "Origin: http://localhost:7860" -d '{}' # 401
curl -i http://localhost:7860/client/ -H "Accept: text/html"                            # 302 -> /login
curl -i -X POST http://localhost:7860/start -H "Origin: http://localhost:7860" \
     -H "Cookie: jarvis_session=eyJ1aWQiOiJ4In0=.abc.def" -d '{}'                       # 401 (forged)
```

Pass: no 200s, and the bot terminal shows no "Starting bot".

## 2. An uninvited account is refused

Sign in with a Google account that isn't in `ALLOWED_EMAILS`. If you don't have one, remove
`krishanbagoria@gmail.com` from `ALLOWED_EMAILS`, restart the bot, and use that account.

Pass: a page saying "You can't sign in to this JARVIS" that names the address, and
`select * from users` in Supabase has no row for it. Put the address back afterwards.

## 3. A signs in and keeps the old data

Window A: open `http://localhost:7860`, sign in as A. You land on the voice client with your
address in the header.

1. Press Connect and ask: "What do you remember about me?" JARVIS should recall facts from
   before the migration.
2. Say: "Remember that my favourite colour is teal." Then: "Add a note: A's private note."
3. Say: "Send a test message saying hello from A." It asks for approval. Say **"no, not yet"**
   so it stays pending.
4. Disconnect.

## 4. B signs in and sees none of it

Window B: open `http://localhost:7860` and sign in as B (it creates B's user row).

1. Connect and ask: "What's my favourite colour?" Pass: JARVIS doesn't know, and never says teal.
2. "List my notes." Pass: none (no "A's private note").
3. "Confirm the pending action." Pass: "There is no pending action" (A's test message is
   neither sent nor cancelled).
4. "What did we talk about last time?" Pass: nothing from A's sessions.
5. "Research the three biggest coffee chains in India", a delegated task. Pass: the worker
   terminal logs the task "for user <B's id>", and JARVIS announces the result to B.
6. Say: "Remember that I work at Acme." Disconnect.

## 5. Back to A

Window A: connect again.

1. "Where do I work?" Pass: JARVIS doesn't know (that was B's fact).
2. Pass: JARVIS never announces B's coffee research.
3. "Confirm the pending action." Pass: A's test message runs now.

## 6. Gmail and Calendar are per user

1. Window B, before connecting Google: ask "What's on my calendar today?" Pass: "your Google
   account isn't connected yet", with a pointer to Settings.
2. Window A: Settings > Connect Google (Gmail + Calendar), pick A's Google account and allow
   access. Pass: Settings shows "Connected as <A's address>".
3. Window B: reload Settings. Pass: still "Not connected". Ask about the calendar by voice.
   Pass: still not connected (B never gets A's mailbox).
4. Window B: connect B's own Google account. Pass: A's Settings still shows A's address, and
   B's shows B's.
5. **Reused state:** right after a successful connect, press the browser's Back button to
   return to the `/auth/google/callback?...` URL and reload it. Pass: "This connect link has
   expired or was already used."
6. **Someone else's state:** in window A, press Connect Google, copy the
   `accounts.google.com` URL from the address bar, and close that tab without finishing. Paste
   the URL into window B and finish the consent there. The link carries A's state, but window
   B is signed in as B. Pass: "This connect link was started by a different JARVIS account",
   and B's connection is unchanged.
7. Window B: Disconnect. Pass: B shows "Not connected" and A is still connected.

## 7. Sessions end properly

1. Window A: Sign out. Then go back to `http://localhost:7860`. Pass: you're sent to the
   login page.
2. Optional, expired session: set `session_hours = 0.01` under `[web]` in `server/jarvis.toml`,
   restart, sign in, and wait a minute. Pass: reloading `/` goes to the login page. Set it back
   to `168` afterwards.
3. Optional, tampered cookie: in DevTools > Application > Cookies, change one character of
   `jarvis_session` and reload. Pass: you're sent to the login page.

## 8. Check the database

```sql
select u.email, count(*) filter (where m.id is not null) as memories
  from users u left join memory m on m.user_id = u.id group by u.email;
select user_id, count(*) from notes group by user_id;
select user_id, provider, account from oauth_tokens;
select count(*) from memory where user_id is null;   -- 0 (the column is NOT NULL)
```

Pass: "teal" belongs to A only, "Acme" to B only, and each Google account sits under its own
user. Finally, `uv run python scripts/check_store.py mamgai75@gmail.com` prints "All good."
