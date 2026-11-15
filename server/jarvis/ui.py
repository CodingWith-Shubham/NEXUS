"""Voice-only tweaks for the Pipecat Playground UI served at /client.

The prebuilt UI is a compiled bundle, so we inject CSS into its index.html instead of editing
site-packages (which a dependency sync would overwrite). JARVIS is audio-only, so the bot video
panel, the camera control and screen sharing are hidden.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

VOICE_ONLY_CSS = """
[data-slot="console-bot-video-panel"],
[data-slot="user-video-control"],
[data-slot="user-screen-control"] { display: none !important; }
[data-slot="console-bot-video-panel"] + [data-slot="resizable-handle"],
[data-slot="resizable-handle"]:has(+ [data-slot="console-bot-video-panel"]) { display: none !important; }
[data-slot="console-bot-audio-panel"] { height: 100% !important; }
"""


class VoiceOnlyUI(StaticFiles):
    """The prebuilt UI with the video/camera/screen-share controls hidden."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html") and response.status_code == 200:
            html = response.body.decode() if hasattr(response, "body") else None
            if html is None:
                with open(response.path, encoding="utf-8") as f:  # FileResponse
                    html = f.read()
            html = html.replace("</head>", f"<style>{VOICE_ONLY_CSS}</style></head>")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return response


def install_voice_only_ui() -> None:
    """Make the runner mount the voice-only UI instead of the stock one."""
    from fastapi.responses import RedirectResponse
    from pipecat.runner import run as runner

    from pipecat_ai_prebuilt.frontend import dist_dir

    def setup(app: FastAPI) -> None:
        app.mount("/client", VoiceOnlyUI(directory=dist_dir, html=True))

        @app.get("/", include_in_schema=False)
        async def root_redirect():
            return RedirectResponse(url="/client/")

    runner._setup_frontend_routes = setup
