# tradedesk/execution/ig/settings.py
"""
Configuration management for the tradedesk library.

Settings are loaded from environment variables.

Required environment variables:
    IG_API_KEY      - Your IG API key
    IG_USERNAME     - Your IG username
    IG_PASSWORD     - Your IG password

Optional environment variables:
    IG_ENVIRONMENT  - "DEMO" or "LIVE" (default: DEMO)
    LOG_LEVEL       - Logging level (default: INFO)
"""

import os
from typing import Literal


class Settings:
    """
    Global settings for the tradedesk library.

    Values are read from environment variables on each access, so env vars
    set after import (e.g. via export_ig_env_from_config) are picked up
    correctly.
    """

    @property
    def ig_api_key(self) -> str:
        return os.getenv("IG_API_KEY", "")

    @property
    def ig_username(self) -> str:
        return os.getenv("IG_USERNAME", "")

    @property
    def ig_password(self) -> str:
        return os.getenv("IG_PASSWORD", "")

    @property
    def ig_environment(self) -> Literal["DEMO", "LIVE"]:
        return os.getenv("IG_ENVIRONMENT", "DEMO").upper()  # type: ignore[return-value]

    def validate(self) -> None:
        missing: list[str] = []
        if not self.ig_api_key:
            missing.append("IG_API_KEY")
        if not self.ig_username:
            missing.append("IG_USERNAME")
        if not self.ig_password:
            missing.append("IG_PASSWORD")

        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Set them in your shell or export them in your runner before importing tradedesk."
            )

        if self.ig_environment not in ("DEMO", "LIVE"):
            raise ValueError(
                f"IG_ENVIRONMENT must be 'DEMO' or 'LIVE', got '{self.ig_environment}'"
            )


# Global settings instance
settings = Settings()
