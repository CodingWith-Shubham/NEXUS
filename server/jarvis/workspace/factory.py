"""Build the calendar/email provider chosen in ``[workspace] provider``."""

from jarvis.config import Config
from jarvis.store import Store
from jarvis.workspace import WorkspaceProvider


def make_workspace_provider(config: Config, store: Store) -> WorkspaceProvider:
    if config.workspace_provider == "google":
        from jarvis.oauth import GoogleOAuth
        from jarvis.workspace.google import GoogleWorkspace

        oauth = GoogleOAuth.from_env(
            store, redirect_uri=config.oauth_redirect_uri, sign_in_url=config.oauth_sign_in_url
        )
        return GoogleWorkspace(oauth, timezone=config.tz())
    if config.workspace_provider == "microsoft":
        from jarvis.workspace.microsoft import MicrosoftGraphWorkspace

        return MicrosoftGraphWorkspace()
    raise ValueError(
        f"[workspace] provider {config.workspace_provider!r} must be 'google' or 'microsoft'"
    )
