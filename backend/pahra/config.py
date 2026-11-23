from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # With the lock on, what happens to lookups from addresses QuietNet doesn't know (e.g. a home whose public IP
    # just changed): "drop" them silently, or "rescue": answer only Dynamic DNS, time and QuietNet's own names, so
    # the router can report its new address, and block the rest at once instead of letting lookups hang.
    adguard_unknown_clients: Literal["drop", "rescue"] = "rescue"
    # How often to re-resolve each home's Dynamic DNS hostname (pahra.home_address)
    ddns_check_s: int = 60

    # Public DNS naming: DoT hostname is <client_id>.<dns_hostname>, DoH is https://<dns_hostname>/dns-query/<client_id>
    dns_hostname: str = "dns.pahra.app"
    vm_ip: str = ""  # public IP of the VM, shown in router / Windows setup steps

    # LLM
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_batch_size: int = 20
    # Or any OpenAI-compatible endpoint instead of Gemini (used when both are set), e.g.
    # LLM_BASE_URL=https://api.aicredits.in/v1  LLM_API_KEY=...  LLM_MODEL=openai/gpt-6-luna
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # Local storage
    db_path: str = "pahra.db"
    lists_cache_dir: str = ".cache/lists"
    # Use the small bundled sample lists instead of downloading (offline dev / tests)
    offline_lists: bool = False

    # Per-account rate limits (pahra.ratelimit). Off only for local scripts that hammer the API.
    rate_limit_enabled: bool = True

    poll_interval_s: int = 30
    event_retention_days: int = 7
    query_log_hours: int = 24  # individual lookups kept for the live view and report charts

    # The operator's token: scripts, and dashboards from before sign-in. It means the first ("seed") account.
    api_token: str = "dev-token"
    # Firebase Authentication: with a project id, people sign in (Google or email link) and each gets their own
    # account that sees only its own devices. Empty = token only (single home).
    firebase_project_id: str = ""
    # Local development only: accept unsigned tokens from the Firebase Auth emulator (e.g. 127.0.0.1:9099).
    firebase_auth_emulator_host: str = ""
    seed_user_email: str = "owner@pahra.local"

    # Companion MCP server (spawned over stdio). Empty = disabled.
    companion_command: str = "pahra-companion"
    companion_enabled: bool = True

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Interactive API docs (/docs, /redoc, /openapi.json). Off on a public server.
    api_docs: bool = True
    # Setup → Home WiFi: detect the visitor's provider from their IP (Team Cymru over DoH).
    provider_lookup: bool = True

    # Langfuse: traces of every AI call + managed prompts. Off unless both keys are set.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"

    @property
    def cache_dir(self) -> Path:
        p = Path(self.lists_cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
