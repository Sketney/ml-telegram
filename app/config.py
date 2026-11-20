from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"

DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5"),
}


class Settings(BaseSettings):
    # env_ignore_empty: пустая строка в .env (ADMIN_ID=) значит «не задано», а не ошибка разбора
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    bot_token: str = ""
    channel_id: str = ""
    admin_id: int = 0
    # Свой Bot API сервер (и мок в тестах). Пусто — официальный api.telegram.org.
    telegram_api_base: str = ""

    llm_provider: str = "fake"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_model_cheap: str = ""

    database_url: str = "sqlite+aiosqlite:///./mlbot.sqlite3"

    timezone: str = "Asia/Tbilisi"
    brand_emoji: str = "🔹"
    channel_signature: str = ""
    quiz_show_answer_option: bool = True

    # Расшифровка голосовых: модель faster-whisper (tiny, base, small, medium). Пусто — выключено.
    whisper_model: str = "small"
    whisper_dir: str = str(ROOT / "whisper")

    schedule_file: str = str(DATA_DIR / "schedule.yaml")
    sources_file: str = str(DATA_DIR / "sources.yaml")

    def model_for(self, cheap: bool = False) -> str:
        explicit = self.llm_model_cheap if cheap else self.llm_model
        if explicit:
            return explicit
        defaults = DEFAULT_MODELS.get(self.llm_provider)
        if not defaults:
            raise RuntimeError(
                f"Для LLM_PROVIDER={self.llm_provider} нужно явно задать LLM_MODEL"
                + (" и LLM_MODEL_CHEAP" if cheap else "")
            )
        return defaults[1] if cheap else defaults[0]

    def problems(self) -> list[str]:
        """Чего не хватает для запуска. Пустой список — можно стартовать."""
        out = []
        if not self.bot_token:
            out.append("BOT_TOKEN не задан")
        if not self.channel_id:
            out.append("CHANNEL_ID не задан")
        if not self.admin_id:
            out.append("ADMIN_ID не задан")
        if self.llm_provider not in ("anthropic", "openai", "fake"):
            out.append(f"LLM_PROVIDER={self.llm_provider}: ожидается anthropic, openai или fake")
        elif self.llm_provider != "fake" and not self.llm_api_key:
            out.append("LLM_API_KEY не задан (или поставь LLM_PROVIDER=fake)")
        if self.llm_provider == "openai" and not self.llm_model:
            out.append("LLM_MODEL не задан (для openai значения по умолчанию нет)")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
