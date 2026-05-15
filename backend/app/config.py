from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SECRET_KEY: str = "dev-secret-change-me"
    API_KEY: str = "dev-api-key"
    DATABASE_URL: str = "sqlite:///./strip.db"
    CORS_ORIGINS: str = "http://localhost:3000"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    SCRAPE_CRON_HOUR: int = 6
    SCRAPE_CRON_MINUTE: int = 0

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1:8b"

    # Twitter
    TWITTER_API_KEY: str = ""
    TWITTER_API_SECRET: str = ""
    TWITTER_ACCESS_TOKEN: str = ""
    TWITTER_ACCESS_SECRET: str = ""

    # LinkedIn
    LINKEDIN_ACCESS_TOKEN: str = ""
    LINKEDIN_AUTHOR_URN: str = ""

    # Facebook
    FACEBOOK_PAGE_ID: str = ""
    FACEBOOK_PAGE_TOKEN: str = ""

    # Instagram
    INSTAGRAM_USER_ID: str = ""
    INSTAGRAM_ACCESS_TOKEN: str = ""

    # WhatsApp
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_RECIPIENTS: str = ""

    # Reddit
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USERNAME: str = ""
    REDDIT_PASSWORD: str = ""
    REDDIT_USER_AGENT: str = "strip-bot/1.0"
    REDDIT_DEFAULT_SUBREDDIT: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_IDS: str = ""

    # Discord
    DISCORD_WEBHOOK_URL: str = ""

    # Mastodon
    MASTODON_BASE_URL: str = "https://mastodon.social"
    MASTODON_ACCESS_TOKEN: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
