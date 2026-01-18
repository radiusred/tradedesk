# tradedesk/config.py
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
from dataclasses import dataclass
from typing import Literal


@dataclass
class Settings:
    """
    Global settings for the tradedesk library.

    Values are loaded from environment variables on initialization.
    Users can override these programmatically if needed:

        from tradedesk.config import settings
        settings.ig_api_key = "custom_key"
    """

    ig_api_key: str = ""
    ig_username: str = ""
    ig_password: str = ""
    ig_environment: Literal["DEMO", "LIVE"] = "DEMO"

    def __post_init__(self) -> None:
        # Populate from environment at import-time.
        self.ig_api_key = os.getenv("IG_API_KEY", "")
        self.ig_username = os.getenv("IG_USERNAME", "")
        self.ig_password = os.getenv("IG_PASSWORD", "")
        self.ig_environment = os.getenv("IG_ENVIRONMENT", self.ig_environment).upper()  # type: ignore

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


# Global settings instance - loaded when module is imported
settings = Settings()
