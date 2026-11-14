"""Microsoft 365 provider (Outlook mail + calendar via Microsoft Graph). NOT IMPLEMENTED YET.

Every method raises :class:`WorkspaceError`, so choosing
``[workspace] provider = "microsoft"`` gives a clear spoken "not available yet"
instead of a crash. To implement it, mirror :class:`jarvis.workspace.google.GoogleWorkspace`:

Auth (Microsoft identity platform, authorization code flow):
    - authorize: https://login.microsoftonline.com/common/oauth2/v2.0/authorize
    - token:     https://login.microsoftonline.com/common/oauth2/v2.0/token
    - scopes: offline_access openid email Mail.Read Mail.Send Calendars.ReadWrite
    - store tokens in oauth_tokens with provider="microsoft" (reuse TokenCipher)
    - add GET /auth/microsoft and /auth/microsoft/callback in jarvis.auth_routes

Graph endpoints (base https://graph.microsoft.com/v1.0):
    list_events      GET  /me/calendarView?startDateTime=...&endDateTime=...
    get_event        GET  /me/events/{id}
    busy_intervals   POST /me/calendar/getSchedule
    create_event     POST /me/events  (isOnlineMeeting: true,
                                       onlineMeetingProvider: "teamsForBusiness")
    update_event     PATCH /me/events/{id}
    cancel_event     POST /me/events/{id}/cancel
    search_messages  GET  /me/messages?$search="..."
    get_message      GET  /me/messages/{id}  (Prefer: outlook.body-content-type="text")
    create_draft     POST /me/messages
    send_message     POST /me/sendMail
    reply            POST /me/messages/{id}/reply  or  /replyAll
"""

from datetime import datetime

from jarvis.workspace import Event, Message, MessageSummary, WorkspaceError

NOT_READY = "Microsoft 365 calendar and email aren't available yet; only Google is set up."


class MicrosoftGraphWorkspace:
    name = "microsoft"

    async def account(self) -> str | None:
        return None

    async def list_events(self, start: datetime, end: datetime, max_results: int = 25) -> list[Event]:
        raise WorkspaceError(NOT_READY)

    async def get_event(self, event_id: str) -> Event:
        raise WorkspaceError(NOT_READY)

    async def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        raise WorkspaceError(NOT_READY)

    async def create_event(self, **kwargs) -> Event:
        raise WorkspaceError(NOT_READY)

    async def update_event(self, event_id: str, **kwargs) -> Event:
        raise WorkspaceError(NOT_READY)

    async def cancel_event(self, event_id: str) -> None:
        raise WorkspaceError(NOT_READY)

    async def search_messages(self, query: str, max_results: int = 5) -> list[MessageSummary]:
        raise WorkspaceError(NOT_READY)

    async def get_message(self, message_id: str) -> Message:
        raise WorkspaceError(NOT_READY)

    async def create_draft(self, **kwargs) -> str:
        raise WorkspaceError(NOT_READY)

    async def send_message(self, **kwargs) -> str:
        raise WorkspaceError(NOT_READY)

    async def reply(self, message_id: str, **kwargs) -> str:
        raise WorkspaceError(NOT_READY)
