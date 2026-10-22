"""Platform settings. Deployment-specific values come from environment variables only;
company-specific behaviour lives in tenant packs (see workforce/tenancy.py)."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKFORCE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./workforce.db"
    tenants_dir: Path = Path("tenants")
    # Comma-separated tenant slugs this deployment serves. Empty = every pack in tenants_dir.
    tenants: str = ""

    environment: str = Field("development", pattern="^(development|staging|production)$")
    secure_cookies: bool = True
    session_hours: int = 12
    allowed_hosts: str = "*"

    worker_concurrency: int = 2
    worker_poll_seconds: float = 2.0
    work_lease_seconds: int = 600

    # The Anthropic SDK reads ANTHROPIC_API_KEY itself; this only switches AI on or off for the deployment.
    ai_enabled: bool = True
    # Encrypts model API keys that owners enter in the panel. Without it, keys can only come from the environment.
    secret_key: str = ""

    @property
    def tenant_slugs(self) -> list[str]:
        return [s.strip() for s in self.tenants.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
