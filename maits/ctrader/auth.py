"""OAuth 2.0 with the cTrader Open API portal: authorize URL, code exchange, refresh, token storage.

Flow: you log in with your cTID in the browser and grant the app access to your accounts. The
portal redirects to your registered redirect URI with `?code=...` (valid for 1 minute). The code
is exchanged for an access token (~30 days) and a refresh token. Refreshing returns a new pair and
invalidates the old one, so the new pair must be saved every time.
"""
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from ctrader_open_api import Auth, EndPoints

from maits.config import Settings


class AuthError(Exception):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    @property
    def seconds_left(self) -> float:
        return self.expires_at - time.time()


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Tokens | None:
        try:
            return Tokens(**json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None

    def save(self, tokens: Tokens) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(tokens), f)
        os.replace(tmp, self.path)  # atomic: a crash can't leave a half-written token file


def authorize_url(settings: Settings, scope: str = "trading") -> str:
    """scope: 'trading' (can place orders) or 'accounts' (read-only)."""
    query = urlencode({"client_id": settings.client_id, "redirect_uri": settings.redirect_uri, "scope": scope})
    return f"{EndPoints.AUTH_URI}?{query}"


def extract_code(text: str) -> str:
    """Accept either the bare code or the full redirect URL pasted from the address bar."""
    text = text.strip()
    if "code=" in text:
        codes = parse_qs(urlparse(text).query).get("code")
        if not codes:
            raise AuthError("No 'code' parameter found in that URL")
        return codes[0]
    return text


def _parse(response: dict) -> Tokens:
    if response.get("errorCode"):
        raise AuthError(f"{response['errorCode']}: {response.get('description', '')}")
    try:
        return Tokens(
            access_token=response["accessToken"],
            refresh_token=response["refreshToken"],
            expires_at=time.time() + float(response["expiresIn"]),
        )
    except KeyError as exc:
        raise AuthError(f"Unexpected token response (missing {exc}): {response}") from exc


def _sdk_auth(settings: Settings) -> Auth:
    return Auth(settings.client_id, settings.client_secret, settings.redirect_uri)


async def exchange_code(settings: Settings, code: str) -> Tokens:
    # the SDK helper uses blocking `requests`, so keep it off the event loop
    return _parse(await asyncio.to_thread(_sdk_auth(settings).getToken, code))


async def refresh_tokens(settings: Settings, tokens: Tokens) -> Tokens:
    return _parse(await asyncio.to_thread(_sdk_auth(settings).refreshToken, tokens.refresh_token))
