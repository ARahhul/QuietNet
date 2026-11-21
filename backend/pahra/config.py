from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables / `.env` (never from Git)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AdGuard Home admin API (reached through the SSH tunnel in the hybrid setup)
    adguard_url: str = "http://127.0.0.1:3000"
    adguard_user: str = "admin"
    adguard_pass: str = "change-me"
    # Restrict who may use the resolver to linked device ClientIDs / IPs (SPEC §8).
    adguard_lock_access: bool = True

    # Public DNS naming: DoT hostname is <client_id>.<dns_hostname>, DoH is https://<dns_hostname>/dns-query/<client_id>
    dns_hostname: str = "dns.pahra.app"
    vm_ip: str = ""  # public IP of the VM, shown in router / Windows setup steps

    # LLM
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_batch_size: int = 20

    # Local storage
    db_path: str = "pahra.db"
    lists_cache_dir: str = ".cache/lists"
    # Use the small bundled sample lists instead of downloading (offline dev / tests)
    offline_lists: bool = False

    poll_interval_s: int = 30
    event_retention_days: int = 7

    # Single-user hackathon auth
    api_token: str = "dev-token"
    seed_user_email: str = "owner@pahra.local"

    # Companion MCP server (spawned over stdio). Empty = disabled.
    companion_command: str = "pahra-companion"
    companion_enabled: bool = True

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cache_dir(self) -> Path:
        p = Path(self.lists_cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
