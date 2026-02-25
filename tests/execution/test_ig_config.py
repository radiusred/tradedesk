# tests/execution/test_ig_config.py
"""
Tests for the config module.
"""

import os
from unittest.mock import patch

import pytest

from tradedesk.execution.ig.settings import Settings, settings


class TestSettings:
    """Test the Settings dataclass."""

    def test_default_values(self):
        """Test that Settings has correct default values."""
        with patch.dict(os.environ, {}, clear=True):
            test_settings = Settings()

            assert test_settings.ig_api_key == ""
            assert test_settings.ig_username == ""
            assert test_settings.ig_password == ""
            assert test_settings.ig_environment == "DEMO"

    def test_environment_variable_loading(self):
        """Test loading values from environment variables."""
        env_vars = {
            "IG_API_KEY": "test-api-key",
            "IG_USERNAME": "test-user",
            "IG_PASSWORD": "test-pass",
            "IG_ENVIRONMENT": "LIVE",
        }

        with patch.dict(os.environ, env_vars):
            test_settings = Settings()

            assert test_settings.ig_api_key == "test-api-key"
            assert test_settings.ig_username == "test-user"
            assert test_settings.ig_password == "test-pass"
            assert test_settings.ig_environment == "LIVE"

    def test_validation_success_via_env(self):
        """Test successful validation when environment variables are set."""
        env_vars = {
            "IG_API_KEY": "test-key",
            "IG_USERNAME": "test-user",
            "IG_PASSWORD": "test-pass",
            "IG_ENVIRONMENT": "DEMO",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            test_settings = Settings()
            # Should not raise an exception
            test_settings.validate()

    def test_validation_missing_values_via_env(self):
        """Test validation raises error for missing values (using env)."""
        env_vars = {
            "IG_USERNAME": "test-user",
            # IG_API_KEY and IG_PASSWORD intentionally omitted
        }

        with patch.dict(os.environ, env_vars, clear=True):
            test_settings = Settings()
            with pytest.raises(ValueError) as exc_info:
                test_settings.validate()

            assert "IG_API_KEY" in str(exc_info.value)
            assert "IG_PASSWORD" in str(exc_info.value)

    def test_validation_invalid_environment_via_env(self):
        """Test validation raises error for invalid environment (using env)."""
        env_vars = {
            "IG_API_KEY": "test-key",
            "IG_USERNAME": "test-user",
            "IG_PASSWORD": "test-pass",
            "IG_ENVIRONMENT": "INVALID",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            test_settings = Settings()
            with pytest.raises(ValueError) as exc_info:
                test_settings.validate()

            assert "IG_ENVIRONMENT" in str(exc_info.value)
            assert "must be 'DEMO' or 'LIVE'" in str(exc_info.value)

    def test_global_settings_instance(self):
        """Test that the global settings instance is created."""
        assert isinstance(settings, Settings)
