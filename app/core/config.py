from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Lead Finder API"
    API_V1_STR: str = "/api/v1"
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]

    RESEND_API_KEY: str = ""
    DEFAULT_FROM_EMAIL: str = "onboarding@resend.dev"
    BRAND_NAME: str = "Lead Finder"
    BRAND_COLOR: str = "#C2410C"
    FRONTEND_URL: str = "http://localhost:3001"
    VERIFY_EMAIL_TOKEN_EXPIRE_HOURS: int = 24
    RESET_PASSWORD_TOKEN_EXPIRE_MINUTES: int = 60

    RUN_LEAD_FINDER: bool = True
    # Secret that encrypts every user's own tool keys (Hunter, Serper, Reoon, AI) in the database.
    # Create one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Losing it makes the saved keys unreadable: users would add them again.
    KEYS_ENCRYPTION_KEY: str = ""

    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
