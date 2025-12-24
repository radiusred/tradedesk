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
from pathlib import Path
from typing import Literal

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv

    cwd_env = Path.cwd() / ".env"    
    if cwd_env.exists():
        load_dotenv(dotenv_path=cwd_env)
    else:
        # Fallback to standard behavior (searches parents)
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
    ig_api_key: str = ""
    ig_username: str = ""
    ig_password: str = ""
    environment: Literal["DEMO", "LIVE"] = "DEMO"
    log_level: str = "INFO"

    def __post_init__(self):
        """
        Refresh values from environment after load_dotenv has run.
        This allows the global 'settings' instance to be populated correctly.
        """
        self.ig_api_key: str = os.getenv("IG_API_KEY", "")
        self.ig_username: str = os.getenv("IG_USERNAME", "")
        self.ig_password: str = os.getenv("IG_PASSWORD", "")
        self.environment = os.getenv("IG_ENVIRONMENT", self.environment) # type: ignore
        self.log_level = os.getenv("LOG_LEVEL", self.log_level)
    
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

def load_strategy_config(config_path: str) -> dict:
    """
    Load strategy configuration from YAML file.
    
    Args:
        config_path: Path to YAML config file
    
    Returns:
        Dictionary containing configuration
    """
    import yaml
    from pathlib import Path
    
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    try:
        with open(config_file) as f:
            config = yaml.safe_load(f)
        
        if config is None:
            raise ValueError(f"Empty config file: {config_path}")
        
        return config
    
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {config_path}: {e}")


# Global settings instance - loaded when module is imported
settings = Settings()
