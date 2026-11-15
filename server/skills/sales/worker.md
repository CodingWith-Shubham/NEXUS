Sales leads: tools act on the user's saved leads; a lead can be named by company. find_leads
queues a lead search; list_leads and get_contact read. approve_lead, reject_lead and
set_lead_status change a lead's status (new, approved, contacted, replied, meeting, rejected).

lookup_contact finds the right person's contact details at an approved lead; it spends a paid
credit and returns awaiting_approval: that is expected, don't retry, say it's waiting for the
user's yes. It refuses leads that aren't approved; don't approve a lead on your own to get
around that.

draft_outreach writes the email (opening with the verified signal and its source) and a LinkedIn
note. send_outreach sends a draft to the contact's email after the user approves and marks the
lead contacted. Never write outreach from scratch with send_email; use these tools so the email
stays grounded in the lead's evidence.
