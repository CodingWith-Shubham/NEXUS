# JARVIS: general personal assistant for a busy executive

## Goal
A voice-first assistant that answers questions, remembers context,
manages calendar and email, does deep research, and runs sales and
marketing tasks. Not tied to one job: each capability is a skill
pack that can be added or removed without touching the core.

## Architecture
- Voice front: Gemini Live. Holds only fast tools (under ~10) and
  one delegate_task tool.
- Worker: a text model running in the background for slow or
  multi-step jobs (research, lead finding, long drafts). It writes
  results to the database and the voice front announces them.
- Skill packs live in skills/<name>/ with their own tools, prompt
  section and tests. The core loads them from a config list.
- Company-specific knowledge (TASC industries, signals, scoring)
  belongs only in skills/sales/, never in the core prompt.

## Phases (build and test one at a time)
1. Core: Supabase tables (memory, tasks, pending_actions,
   audit_log, oauth_tokens). Tool registry and skill loader.
   Approval gate with confirm_action and cancel_action.
   delegate_task plus a background worker and get_task_status.
2. General assistant: web search for current information,
   remember and recall tools for long-term memory (preferences,
   people, ongoing projects), notes and reminders.
3. Calendar and email: provider sign-in, list_events,
   find_free_time, create_meeting, draft_email, send_email.
4. Deep research: worker job that searches, reads sources, and
   returns a cited report saved to the database.
5. Sales pack: find and verify leads, score, SignalHire contact
   reveal, outreach drafts.
6. Marketing pack: LinkedIn post, comment, react; content drafts;
   content calendar.
7. Frontend: Next.js with the Pipecat React client. Panels for
   conversation, pending approvals, tasks and results.
8. Phone: inbound number for the owner with allow-list and PIN.