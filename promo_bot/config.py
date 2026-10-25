import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    token: str
    db_path: str
    api_url: str
    timezone: ZoneInfo
    grand_prize: str
    max_chances: int
    final_winners: int
    final_reserves: int
    banner_token: str

    @classmethod
    def from_env(cls, require_token: bool = True) -> "Settings":
        load_env()
        token = os.getenv("MAX_BOT_TOKEN", "").strip()
        if require_token and not token:
            raise RuntimeError("MAX_BOT_TOKEN is not set")
        return cls(
            token=token,
            db_path=os.getenv("DB_PATH", "data/promo_bot.db"),
            api_url=os.getenv("MAX_API_URL", "https://platform-api2.max.ru"),
            timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
            grand_prize=os.getenv("GRAND_PRIZE", "5 комплектов по 2 билета"),
            max_chances=int(os.getenv("MAX_CHANCES", "10")),
            final_winners=int(os.getenv("FINAL_WINNERS", "5")),
            final_reserves=int(os.getenv("FINAL_RESERVES", "5")),
            banner_token=os.getenv("WELCOME_BANNER_TOKEN", "").strip(),
        )

