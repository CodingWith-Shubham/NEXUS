import pytest
from google.genai import errors

from jarvis.genai_retry import generate_content


def _err(code):
    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


class Flaky:
    def __init__(self, outcomes):
        self.aio = type("Aio", (), {})()
        self.aio.models = self
        self.outcomes = list(outcomes)
        self.models = []

    async def generate_content(self, *, model, contents, config):
        self.models.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_retries_overload_then_succeeds():
    client = Flaky([_err(503), "ok"])
    assert await generate_content(client, model="a", contents="x", backoff_secs=0) == "ok"
    assert client.models == ["a", "a"]


async def test_falls_back_after_retries():
    client = Flaky([_err(503), _err(503), "ok"])
    result = await generate_content(
        client, model="a", contents="x", fallback_model="b", attempts=2, backoff_secs=0
    )
    assert result == "ok" and client.models == ["a", "a", "b"]


async def test_quota_skips_to_fallback_without_retrying():
    client = Flaky([_err(429), "ok"])
    result = await generate_content(
        client, model="a", contents="x", fallback_model="b", backoff_secs=0
    )
    assert result == "ok" and client.models == ["a", "b"]


async def test_quota_everywhere_raises():
    client = Flaky([_err(429), _err(429)])
    with pytest.raises(errors.APIError):
        await generate_content(client, model="a", contents="x", fallback_model="b", backoff_secs=0)
    assert client.models == ["a", "b"]


async def test_bad_request_is_not_retried():
    client = Flaky([_err(400)])
    with pytest.raises(errors.APIError):
        await generate_content(client, model="a", contents="x", fallback_model="b", backoff_secs=0)
    assert client.models == ["a"]
