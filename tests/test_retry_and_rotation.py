import requests
import pytest
from core.jlcpcb_searcher import JlcpcbSearcher, JlcpcbSearchResult
from core.digikey_searcher import DigiKeySearcher, DigiKeySearchResult
from core.logger import mask_secret


class _MockResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=resp)

    def json(self):
        return self.payload


def test_jlcpcb_retry_backoff_and_jitter(monkeypatch):
    sleep_calls = []
    searcher = JlcpcbSearcher(
        "app", "access", "secret",
        _sleep_fn=lambda delay: sleep_calls.append(delay),
        max_retries=3,
    )

    attempt_count = 0
    def mock_post(url, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count <= 2:
            return _MockResponse(503, headers={"Retry-After": "2"})
        return _MockResponse(200, {"code": 200, "data": [{"componentCode": "C1234"}]})

    monkeypatch.setattr(searcher.session, "post", mock_post)

    result = searcher.search_lcsc("C1234", "TEST-MPN", refresh=True)
    assert attempt_count == 3
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == 2.0
    assert sleep_calls[1] == 2.0
    assert result.found is True


def test_digikey_retry_on_500_without_credential_rotation(monkeypatch):
    sleep_calls = []
    searcher = DigiKeySearcher(
        _sleep_fn=lambda delay: sleep_calls.append(delay),
        max_retries=3,
    )
    searcher._credentials = [("client-1", "secret-1"), ("client-2", "secret-2")]

    token_attempts = 0
    def mock_token_post(url, **kwargs):
        nonlocal token_attempts
        token_attempts += 1
        if token_attempts <= 2:
            return _MockResponse(500)
        return _MockResponse(200, {"access_token": "token-1", "expires_in": 1800})

    monkeypatch.setattr(searcher.session, "post", mock_token_post)

    token = searcher._get_access_token()
    assert token == "token-1"
    assert token_attempts == 3
    assert searcher._active_cred_index == 0  # Did not rotate credential for 500
    assert len(sleep_calls) == 2


def test_digikey_credential_rotation_on_429_and_auth_error(monkeypatch):
    searcher = DigiKeySearcher(_sleep_fn=lambda _: None)
    searcher._credentials = [("client-1", "secret-1"), ("client-2", "secret-2")]

    token_calls = []
    def mock_token_post(url, **kwargs):
        client_id = kwargs.get("data", {}).get("client_id")
        token_calls.append(client_id)
        if client_id == "client-1":
            return _MockResponse(429)
        return _MockResponse(200, {"access_token": "token-2", "expires_in": 1800})

    monkeypatch.setattr(searcher.session, "post", mock_token_post)

    token = searcher._get_access_token()
    assert token == "token-2"
    assert token_calls == ["client-1", "client-2"]
    assert searcher._active_cred_index == 1  # Successfully rotated to next credential


def test_mask_secret_helper():
    assert mask_secret("") == "(empty)"
    assert mask_secret("short") == "***"
    assert mask_secret("1234567890abcdef") == "123***def"
