"""Environment configuration for the API, database and model provider."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL', ''))
    timezone: str = field(default_factory=lambda: os.getenv('APP_TIMEZONE', 'Europe/Moscow'))
    fixed_now: str = field(default_factory=lambda: os.getenv('APP_FIXED_NOW', ''))
    llm_base_url: str = field(default_factory=lambda: os.getenv('LLM_BASE_URL', 'http://localhost:8090/v1'))
    llm_api_key: str = field(default_factory=lambda: os.getenv('LLM_API_KEY', ''))
    llm_model: str = field(default_factory=lambda: os.getenv('LLM_MODEL', 'GigaChat-3-Lightning'))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv('LLM_TIMEOUT_SECONDS', '45')))
    frontend_origin: str = field(default_factory=lambda: os.getenv('FRONTEND_ORIGIN', 'http://localhost:5174'))
    cookie_secure: bool = field(default_factory=lambda: os.getenv('COOKIE_SECURE', '0') == '1')
    unified_operations: bool = field(
        default_factory=lambda: os.getenv('UNIFIED_OPERATIONS_ENABLED', '1') == '1'
    )
    pipeline_logging: bool = field(
        default_factory=lambda: os.getenv('PIPELINE_LOG_ENABLED', '1') == '1'
    )

