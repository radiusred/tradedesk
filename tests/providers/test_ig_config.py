# tests/test_config.py
"""
Tests for the config module.
"""
import os
from unittest.mock import patch, mock_open
import pytest
from tradedesk.providers.ig.settings import Settings, settings

class TestSettings:
    """Test the Settings dataclass."""
    
    def test_default_values(self):
        """Test that Settings has correct default values."""
        # Temporarily clear environment variables
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
    
    def test_validation_success(self):
        """Test successful validation with all required values."""
        test_settings = Settings()
        test_settings.ig_api_key = "test-key"
        test_settings.ig_username = "test-user"
        test_settings.ig_password = "test-pass"
        test_settings.ig_environment = "DEMO"
        
        # Should not raise an exception
        test_settings.validate()
    
    def test_validation_missing_values(self):
        """Test validation raises error for missing values."""
        test_settings = Settings()
        test_settings.ig_api_key = ""
        test_settings.ig_username = "test-user"
        test_settings.ig_password = ""
        
        with pytest.raises(ValueError) as exc_info:
            test_settings.validate()
        
        assert "IG_API_KEY" in str(exc_info.value)
        assert "IG_PASSWORD" in str(exc_info.value)
    
    def test_validation_invalid_environment(self):
        """Test validation raises error for invalid environment."""
        test_settings = Settings()
        test_settings.ig_api_key = "test-key"
        test_settings.ig_username = "test-user"
        test_settings.ig_password = "test-pass"
        test_settings.ig_environment = "INVALID"  # Invalid value
        
        with pytest.raises(ValueError) as exc_info:
            test_settings.validate()
        
        assert "IG_ENVIRONMENT" in str(exc_info.value)
        assert "must be 'DEMO' or 'LIVE'" in str(exc_info.value)
        
    def test_global_settings_instance(self):
        """Test that the global settings instance is created."""
        assert isinstance(settings, Settings)
