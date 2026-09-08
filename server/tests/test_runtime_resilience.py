import asyncio
import json

from starlette.requests import Request

import app as app_module


class _ProfileResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({
            "personalInfo": {
                "id": 151,
                "login": "contestant",
                "jaccountAccount": "contestant",
                "name": "参赛者",
            }
        }).encode()


def _request(cookie: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "headers": [(b"cookie", cookie.encode())],
    })


def test_contest_user_profile_is_cached(monkeypatch):
    calls = 0

    def fake_urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _ProfileResponse()

    monkeypatch.setenv("SENTRY_DUEL_AUTH_REQUIRED", "1")
    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    app_module.contest_user_cache.clear()

    first = asyncio.run(app_module._contest_user(_request("session=test")))
    second = asyncio.run(app_module._contest_user(_request("session=test")))

    assert first == second
    assert calls == 1


def test_process_wait_does_not_use_worker_thread(monkeypatch):
    polls = iter([None, None, 0])

    class FakeProcess:
        returncode = 0

        def poll(self):
            return next(polls)

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(app_module.asyncio, "sleep", no_delay)
    result = asyncio.run(app_module._wait_for_process(FakeProcess()))

    assert result == 0
