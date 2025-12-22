# tradedesk/config.py
"""
Configuration management for the tradedesk library.

Settings are loaded from environment variables or a .env file.

Required environment variables:
    IG_API_KEY      - Your IG API key
    IG_USERNAME     - Your IG username
    IG_PASSWORD     - Your IG password

Optional environment variables:
    IG_ENVIRONMENT  - "DEMO" or "LIVE" (default: DEMO)
    LOG_LEVEL       - Logging level (default: INFO)

Example .env file:
    IG_API_KEY=your_api_key_here
    IG_USERNAME=your_username
    IG_PASSWORD=your_password
    IG_ENVIRONMENT=DEMO
    LOG_LEVEL=DEBUG
"""

import os
from dataclasses import dataclass
from typing import Literal

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed, just use environment variables
    pass


@dataclass
class Settings:
    """
    Global settings for the tradedesk library.
    
    Values are loaded from environment variables on initialization.
    Users can override these programmatically if needed:
    
        from tradedesk.config import settings
        settings.ig_api_key = "custom_key"
    """
    
    # IG API credentials
    ig_api_key: str = os.getenv("IG_API_KEY", "")
    ig_username: str = os.getenv("IG_USERNAME", "")
    ig_password: str = os.getenv("IG_PASSWORD", "")
    
    # Environment: "DEMO" or "LIVE"
    environment: Literal["DEMO", "LIVE"] = os.getenv("IG_ENVIRONMENT", "DEMO")  # type: ignore
    
    # Logging level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    def validate(self) -> None:
        """
        Validate that required settings are present.
        
        Raises:
            ValueError: If required settings are missing or invalid
        """
        missing = []
        if not self.ig_api_key:
            missing.append("IG_API_KEY")
        if not self.ig_username:
            missing.append("IG_USERNAME")
        if not self.ig_password:
            missing.append("IG_PASSWORD")
        
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please set these in your .env file or environment."
            )
        
        if self.environment not in ("DEMO", "LIVE"):
            raise ValueError(
                f"IG_ENVIRONMENT must be 'DEMO' or 'LIVE', got '{self.environment}'"
            )


# Global settings instance - loaded when module is imported
settings = Settings()
