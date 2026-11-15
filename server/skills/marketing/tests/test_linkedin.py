"""The LinkedIn layer: text escaping, post URLs, the official API calls, and the OAuth connection."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from skills.marketing.linkedin import LinkedInError, escape_little, parse_post_url
from skills.marketing.linkedin.connection import EXPIRED, LinkedInUnavailable
from skills.marketing.linkedin.official import OfficialLinkedIn
from skills.marketing.linkedin.third_party import ThirdPartyLinkedIn
from skills.marketing.tests.conftest import connect, make_connection, oauth_http

# Text and URLs


@pytest.mark.parametrize("text,expected", [
    ("Plain text.", "Plain text."),
    ("Growth (2026) is *real* & [big]", r"Growth \(2026\) is \*real\* & \[big\]"),
    ("Ask me @ the event | 10_000 ~ <maybe>", r"Ask me \@ the event \| 10\_000 \~ \<maybe\>"),
    ("Big news #hiring #KSA", "Big news #hiring #KSA"),  # hashtags stay hashtags
    ("We're #1 in C# and a#b", r"We're #1 in C\# and a\#b"),
    ("back\\slash {braces}", r"back\\slash \{braces\}"),
])
def test_little_text_escaping(text, expected):
    assert escape_little(text) == expected


@pytest.mark.parametrize("url,urn", [
    ("https://www.linkedin.com/feed/update/urn:li:activity:7254321098765432109/",
     "urn:li:activity:7254321098765432109"),
    ("https://www.linkedin.com/feed/update/urn:li:share:7254321098765432109",
     "urn:li:share:7254321098765432109"),
    ("https://www.linkedin.com/feed/update/urn:li:ugcPost:7254321098765432109/?utm_source=x",
     "urn:li:ugcPost:7254321098765432109"),
    ("https://www.linkedin.com/posts/jane-doe_hiring-riyadh-activity-7254321098765432109-AbCd",
     "urn:li:activity:7254321098765432109"),
    ("https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7254321098765432109",
     "urn:li:activity:7254321098765432109"),
    ("urn:li:share:7254321098765432109", "urn:li:share:7254321098765432109"),
    ("https://evil.example/feed/update/urn:li:activity:7254321098765432109/", None),
    ("https://www.linkedin.com/in/jane-doe/", None),
    ("", None),
])
def test_post_urls(url, urn):
    assert parse_post_url(url) == urn


# The official API (HTTP faked)


def api(status=201, headers=None, body=None):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers or {}, json=body if body is not None else {})

    return OfficialLinkedIn(api_version="202610",
                            http=httpx.AsyncClient(transport=httpx.MockTransport(handler))), calls


async def test_create_post_request_and_response():
    provider, calls = api(headers={"x-restli-id": "urn:li:share:7300000000000000009"})
    result = await provider.create_post("tok", author="urn:li:person:abc123", text=r"Hello \(world\)")
    [request] = calls
    assert str(request.url) == "https://api.linkedin.com/rest/posts" and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Linkedin-Version"] == "202610"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert json.loads(request.content) == {
        "author": "urn:li:person:abc123", "commentary": r"Hello \(world\)", "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    assert result.urn == "urn:li:share:7300000000000000009"
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:7300000000000000009/"
    assert result.response["x-restli-id"] == result.urn and "tok" not in json.dumps(result.response)


async def test_comment_and_reaction_requests():
    provider, calls = api(body={"commentUrn": "urn:li:comment:(urn:li:activity:1,2)"})
    await provider.comment("tok", actor="urn:li:person:abc123", target="urn:li:activity:7254321098765432109",
                           text="Great point")
    assert unquote(calls[0].url.raw_path.decode()) == \
        "/rest/socialActions/urn:li:activity:7254321098765432109/comments"
    assert "urn%3Ali%3Aactivity%3A7254321098765432109" in calls[0].url.raw_path.decode()
    assert json.loads(calls[0].content) == {"actor": "urn:li:person:abc123",
                                            "object": "urn:li:activity:7254321098765432109",
                                            "message": {"text": "Great point"}}
    await provider.react("tok", actor="urn:li:person:abc123", target="urn:li:share:7", reaction="PRAISE")
    assert calls[1].url.path == "/rest/reactions"
    assert parse_qs(urlparse(str(calls[1].url)).query)["actor"] == ["urn:li:person:abc123"]
    assert json.loads(calls[1].content) == {"root": "urn:li:share:7", "reactionType": "PRAISE"}


@pytest.mark.parametrize("status,kind", [(401, "expired"), (403, "denied"), (404, "not_found"),
                                         (429, "rate_limited"), (422, "invalid"), (500, "error")])
async def test_errors_are_classified(status, kind):
    provider, _ = api(status=status, body={"message": "nope", "status": status})
    with pytest.raises(LinkedInError) as caught:
        await provider.create_post("tok", author="urn:li:person:a", text="x")
    assert caught.value.kind == kind and caught.value.response["status"] == status
    if kind == "expired":
        assert str(caught.value) == "Your LinkedIn connection has expired, please reconnect it in Settings."


async def test_third_party_is_only_a_stub():
    with pytest.raises(LinkedInError, match="isn't built"):
        await ThirdPartyLinkedIn().create_post("tok", author="urn:li:person:a", text="x")


# The OAuth connection


def test_authorization_url_asks_for_the_four_scopes():
    url = make_connection(None).authorization_url("signed-state")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert q["scope"] == ["openid profile email w_member_social"] and q["state"] == ["signed-state"]
    assert q["redirect_uri"] == ["http://localhost:7860/auth/linkedin/callback"]
    assert q["response_type"] == ["code"] and q["client_id"] == ["li-client"]


async def test_callback_stores_encrypted_tokens_and_the_member_urn(env):
    http, calls = oauth_http()
    connection = make_connection(env.store, http)
    who = await connection.handle_callback("code-1")
    assert who["person_urn"] == "urn:li:person:abc123"
    form = parse_qs(calls[0].content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["code-1"]
    assert form["redirect_uri"] == ["http://localhost:7860/auth/linkedin/callback"]
    assert calls[1].headers["Authorization"] == "Bearer li-access-1"
    row = await env.store.get_oauth_token("linkedin")
    assert row["user_id"] == env.store.user_id and "li-access-1" not in json.dumps(row)  # encrypted
    assert row["metadata"]["person_urn"] == "urn:li:person:abc123"
    assert await connection.token() == ("li-access-1", "urn:li:person:abc123")
    status = await connection.status()
    assert status["connected"] and not status["expired"] and not status["warn"]
    assert 58 <= status["days_left"] <= 60


async def test_callback_without_posting_permission_is_refused(env):
    http, _ = oauth_http(scope="openid profile email")
    with pytest.raises(LinkedInUnavailable, match="w_member_social"):
        await make_connection(env.store, http).handle_callback("code-1")
    assert await env.store.get_oauth_token("linkedin") is None


async def test_expiry_warning_and_expired(env):
    connection = make_connection(env.store)
    await connect(env.store, expires_in_days=5)
    status = await connection.status()
    assert status["warn"] and status["days_left"] in (4, 5) and not status["expired"]
    await connect(env.store, expires_in_days=-1)
    assert (await connection.status())["expired"]
    with pytest.raises(LinkedInUnavailable) as caught:
        await connection.token()
    assert str(caught.value) == EXPIRED and caught.value.expired


async def test_refresh_token_is_used_when_linkedin_gave_one(env):
    http, calls = oauth_http()
    connection = make_connection(env.store, http)
    await connect(env.store, expires_in_days=-1, refresh="li-refresh-1")
    token, _ = await connection.token()
    assert token == "li-access-1"
    form = parse_qs(calls[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["li-refresh-1"]
    row = await env.store.get_oauth_token("linkedin")
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(UTC) + timedelta(days=50)


async def test_another_app_or_user_doesnt_count_as_connected(env):
    await connect(env.store)
    other_app = make_connection(env.store)
    other_app._client_id = "some-other-app"
    assert not (await other_app.status())["connected"]
    stranger = make_connection(env.db.for_user("00000000-0000-4000-8000-0000000000bb"))
    assert not (await stranger.status())["connected"]
    with pytest.raises(LinkedInUnavailable, match="isn't connected"):
        await stranger.token()
