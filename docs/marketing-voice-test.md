# Marketing pack: voice test

A live test of Phase 6, about 20 minutes. Steps 4, 6 and 8 publish on your real LinkedIn
account. Use a test post you're happy to delete, or set the post to "Connections only"
afterwards. Every publish asks for your yes first.

## Before you start

1. **LinkedIn app** (https://www.linkedin.com/developers/apps, your app):
   - **Products** tab: "Sign In with LinkedIn using OpenID Connect" and "Share on LinkedIn" are
     both added.
   - **Auth** tab > Authorized redirect URLs: add exactly
     `http://localhost:7860/auth/linkedin/callback`.
2. Run `supabase/migrations/0006_marketing.sql` in the Supabase SQL editor.
3. Restart `uv run bot.py` **and** `uv run worker.py`. The worker publishes scheduled posts.
4. Use headphones, sign in at `http://localhost:7860`, and connect the voice client.

## 1. Connect LinkedIn

On `/settings`, press **Connect LinkedIn** and approve on LinkedIn.

- You come back to Settings: "LinkedIn is connected. Connected as <your name>", with when it
  expires (60 days from now).
- Right after connecting, JARVIS checks what LinkedIn allows. The Settings section shows
  **Comments** and **Reactions** as either "allowed" or "not allowed for this app". Nothing is
  posted by this check.

## 2. Marketing profile

On `/marketing/profile`, fill in your voice and tone, topics, audience, things never to say,
hashtag preferences, and 3 to 5 of your own posts, separated by `---`. Save.

## 3. Draft a post

Say: **"Draft a LinkedIn post about how we help companies hire faster in Riyadh."**

- JARVIS reads a draft in your voice and asks whether to post, schedule or change it.
- `/content` shows it under Drafts. It contains no numbers or client names you didn't give.

Say: **"Draft a LinkedIn post from my latest research report."**

- The draft names its source, or ends with a "Source:" line and link.

## 4. Post it (publishes on LinkedIn)

Say: **"Post it."**

- JARVIS reads the **exact text** back word for word and asks you to confirm. Say **"Yes."**
- The post appears on your LinkedIn profile. `/content` lists it under Published with an
  "open on LinkedIn" link.
- Saying **"No"** instead publishes nothing.

## 5. Schedule one

- **"Draft a post about onboarding new hires."**, then **"Schedule it for tomorrow at 9am."**
- JARVIS reads the exact text and the time, then asks. Say yes. `/content` shows it under
  Scheduled.
- **"What posts are scheduled?"** lists it. **"Move it to 4pm."** reschedules it without asking
  again: the text didn't change.
- On `/content`, edit its text and save. It goes back to Drafts, and scheduling it again asks
  for a fresh yes.
- To see it publish without waiting, schedule a post for **"in 5 minutes"**. About 5 minutes
  later the worker publishes it. Disconnect and reconnect the voice client, and JARVIS
  mentions it was published.
- **"Cancel the scheduled post."** moves it back to drafts.

## 6. Comment on a post

Copy the link of a post (yours or a colleague's, who knows): "..." menu > "Copy link to post".

Say: **"Comment on this post: <paste link> saying 'Great point, thanks for sharing.'"** You
can type the link in the voice client's chat, or ask JARVIS to use the link you paste.

- **If comments are allowed:** JARVIS reads the exact comment and asks. Say yes, and the
  comment appears on the post.
- **If not allowed:** JARVIS says LinkedIn doesn't let this app comment. The text is saved on
  `/content` with a Copy button, for you to paste yourself.

## 7. React

Say: **"React 'insightful' to <link>."** It works the same way: it asks first if allowed, or
tells you to react yourself if not.

## 8. Limits it should state plainly

- **"What's on my LinkedIn feed?"**: JARVIS says LinkedIn's API doesn't let apps read the feed.
- **"Send a LinkedIn message to Priya."**: not possible; it says so.

## 9. Expiry (optional)

To check the expiry handling without waiting 60 days, set the connection to expire tomorrow in
Supabase:

```sql
update oauth_tokens set expires_at = now() + interval '1 day' where provider = 'linkedin';
```

Reconnect the voice client. JARVIS reminds you the LinkedIn connection expires soon, and
Settings shows the warning. Set it to the past (`now() - interval '1 minute'`) and ask to
post: JARVIS says "your LinkedIn connection has expired, please reconnect". A scheduled post
due then is skipped, marked "Not published", and mentioned at your next session. Reconnect
on Settings to fix it.

## The record

```sql
select event, detail->'linkedin_response'->>'status' as status, created_at
  from audit_log where tool = 'linkedin' order by created_at desc limit 10;
```

Every publish, comment, reaction, failure and capability check is listed with LinkedIn's
response.
