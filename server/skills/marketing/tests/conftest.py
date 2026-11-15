"""Shared fakes for the marketing pack tests. No network: LinkedIn and the model are faked."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import skills.marketing as marketing
from jarvis.app import build_core
from jarvis.config import Config
from jarvis.llm import ChatReply
from jarvis.oauth import TokenCipher
from jarvis.skills import ToolContext
from jarvis.store import MemoryDatabase
from jarvis.worker import TaskWorker
from skills.marketing.linkedin import LinkedInError, Published, post_url
from skills.marketing.linkedin.connection import LinkedInConnection
from skills.marketing.linkedin.official import OfficialLinkedIn
from skills.marketing.profile import MarketingProfile

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="  # fixed: conftest may load as two modules
CLIENT_ID = "li-client"

PROFILE = {
    "voice": "Plain-spoken and warm, first person, short sentences.",
    "topics": ["hiring in Saudi Arabia", "workforce planning"],
    "audience": "HR leaders in the Gulf",
    "never_say": ["guaranteed results", "best in class"],
    "hashtags": "2-3 relevant hashtags at the end",
    "sample_posts": [
        "Hiring at scale is a logistics problem before it's a people problem. Here's what we learned this quarter.",
        "Three things every HR team in Riyadh asks us about contract staffing, and our honest answers.",
        "We don't talk enough about onboarding. A great hire with a bad first week is a lost hire.",
    ],
}


@pytest.fixture(autouse=True)
def no_real_linkedin(monkeypatch):
    original_post = OfficialLinkedIn._post
    original_request = LinkedInConnection._request

    async def guarded_post(self, path, token, body):
        if self._http is None:
            raise AssertionError(f"a test tried to call the real LinkedIn API ({path})")
        return await original_post(self, path, token, body)

    async def guarded_request(self, method, url, **kwargs):
        if self._http is None:
            raise AssertionError(f"a test tried to call the real LinkedIn OAuth ({url})")
        return await original_request(self, method, url, **kwargs)

    monkeypatch.setattr(OfficialLinkedIn, "_post", guarded_post)
    monkeypatch.setattr(LinkedInConnection, "_request", guarded_request)


class FakeLinkedIn:
    """Stands in for the official provider; records every call."""

    name = "fake"

    def __init__(self, *, comment_error=None, react_error=None, post_error=None):
        self.calls: list[tuple] = []
        self.comment_error = comment_error
        self.react_error = react_error
        self.post_error = post_error

    async def create_post(self, token, *, author, text):
        self.calls.append(("post", token, author, text))
        if self.post_error:
            raise self.post_error
        urn = "urn:li:share:7300000000000000001"
        return Published(urn=urn, url=post_url(urn), status=201,
                         response={"status": 201, "x-restli-id": urn, "body": {}})

    async def comment(self, token, *, actor, target, text):
        self.calls.append(("comment", token, actor, target, text))
        if self.comment_error:
            raise self.comment_error
        return Published(urn="urn:li:comment:(x,1)", url=post_url(target), status=201,
                         response={"status": 201, "x-restli-id": "1", "body": {}})

    async def react(self, token, *, actor, target, reaction):
        self.calls.append(("react", token, actor, target, reaction))
        if self.react_error:
            raise self.react_error
        return Published(urn=f"urn:li:reaction:({actor},{target})", url=post_url(target), status=201,
                         response={"status": 201, "body": {"reactionType": reaction}})


def denied(what="comment"):
    return LinkedInError(f"LinkedIn doesn't allow this app to {what}.", kind="denied", status=403,
                         response={"status": 403, "body": {"message": "ACCESS_DENIED"}})


def not_found():
    return LinkedInError("LinkedIn couldn't find that post.", kind="not_found", status=404,
                         response={"status": 404, "body": {}})


class FakeDraftLLM:
    chain = []

    def __init__(self, texts=None):
        self.texts = list(texts or ["Hiring in Riyadh is picking up. Here's what I'm seeing. #hiring"])
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return ChatReply(text=json.dumps({"text": text}), model="fake")


def oauth_http(*, scope="openid profile email w_member_social", sub="abc123", token_status=200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url).startswith("https://www.linkedin.com/oauth/v2/accessToken"):
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_request"})
            return httpx.Response(200, json={"access_token": "li-access-1", "expires_in": 5184000,
                                             "scope": scope})
        if str(request.url).startswith("https://api.linkedin.com/v2/userinfo"):
            return httpx.Response(200, json={"sub": sub, "name": "Sam Example",
                                             "email": "sam@example.com"})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def make_connection(store, http=None):
    return LinkedInConnection(store, cipher=TokenCipher(KEY), client_id=CLIENT_ID,
                              client_secret="li-secret",
                              redirect_uri="http://localhost:7860/auth/linkedin/callback",
                              warn_days=7, http=http)


async def connect(store, *, expires_in_days=50, capabilities=None, refresh=None):
    """A stored LinkedIn connection, as the callback would leave it."""
    cipher = TokenCipher(KEY)
    await store.save_oauth_token("linkedin", "sam@example.com", {
        "access_token": cipher.encrypt("li-access-1"),
        "refresh_token": cipher.encrypt(refresh) if refresh else None,
        "expires_at": (datetime.now(UTC) + timedelta(days=expires_in_days)).isoformat(),
        "metadata": {"member_id": "abc123", "person_urn": "urn:li:person:abc123", "name": "Sam Example",
                     "client_id": CLIENT_ID, "capabilities": capabilities or {}},
    })


class Env:
    def __init__(self, monkeypatch, *, provider=None, llm=None, profile=True,
                 user="00000000-0000-4000-8000-0000000000aa"):
        self.db = MemoryDatabase()
        config = Config(skills=["marketing", "research", "sales"], timezone="Asia/Kolkata")
        self.core = build_core(config)
        self.provider = provider or FakeLinkedIn()
        self.llm = llm or FakeDraftLLM()
        monkeypatch.setattr(marketing, "connection_factory", lambda store, config: make_connection(store))
        monkeypatch.setattr(marketing, "provider_factory", lambda config: self.provider)
        monkeypatch.setattr(marketing, "load_config", lambda: config)
        self.store = self.db.for_user(user)
        if profile:
            self.db.tables["marketing_profiles"].append(
                {"id": "11111111-1111-4111-8111-111111111111", "user_id": user,
                 "profile": MarketingProfile.from_dict(PROFILE).to_dict()})
        self.ctx = self.make_ctx(self.store)
        self.worker = TaskWorker(self.db, self.core.registry, llm=self.llm, system_instruction="sys",
                                 config=config)

    def make_ctx(self, store, source="voice"):
        return ToolContext(store=store, registry=self.core.registry, source=source, session_id="s1",
                           config=self.core.config, llm=self.llm)

    async def call(self, name, ctx=None, **args):
        ctx = ctx or self.ctx
        return await ctx.registry.execute(name, args, ctx)


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)
