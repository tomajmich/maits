"""Settings, read from environment variables (and a .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    env: str  # "demo" | "live"
    account_id: int | None
    token_file: Path
    max_lots: float
    allowed_symbols: frozenset[str]  # empty = any
    api_key: str
    webhook_secret: str
    api_host: str
    api_port: int

    @property
    def is_live(self) -> bool:
        return self.env == "live"

    def require_app_credentials(self) -> None:
        missing = [
            name
            for name, value in [
                ("CTRADER_CLIENT_ID", self.client_id),
                ("CTRADER_CLIENT_SECRET", self.client_secret),
                ("CTRADER_REDIRECT_URI", self.redirect_uri),
            ]
            if not value
        ]
        if missing:
            raise ConfigError(f"Missing {', '.join(missing)} - copy .env.example to .env and fill it in.")


def load_settings() -> Settings:
    load_dotenv()
    e = os.environ.get
    env = e("CTRADER_ENV", "demo").strip().lower()
    if env not in ("demo", "live"):
        raise ConfigError("CTRADER_ENV must be 'demo' or 'live'")
    try:
        account_id = int(e("CTRADER_ACCOUNT_ID", "").strip() or 0) or None
        max_lots = float(e("MAITS_MAX_LOTS", "0.10"))
        api_port = int(e("MAITS_API_PORT", "8000"))
    except ValueError as exc:
        raise ConfigError(f"Invalid numeric setting: {exc}") from exc
    return Settings(
        client_id=e("CTRADER_CLIENT_ID", "").strip(),
        client_secret=e("CTRADER_CLIENT_SECRET", "").strip(),
        redirect_uri=e("CTRADER_REDIRECT_URI", "").strip(),
        env=env,
        account_id=account_id,
        token_file=Path(e("MAITS_TOKEN_FILE", "tokens.json")),
        max_lots=max_lots,
        allowed_symbols=frozenset(s.strip().upper() for s in e("MAITS_ALLOWED_SYMBOLS", "").split(",") if s.strip()),
        api_key=e("MAITS_API_KEY", "").strip(),
        webhook_secret=e("MAITS_WEBHOOK_SECRET", "").strip(),
        api_host=e("MAITS_API_HOST", "127.0.0.1").strip(),
        api_port=api_port,
    )
