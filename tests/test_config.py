"""Tests for config.py — Config singleton and accessors."""

import logging
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from wiz_sdk.config import Config, generate_default_config, DEFAULT_WIZ_DIR


class TestConfigLoading:
    def test_load_from_dict(self, minimal_config):
        """Loading a config dict directly should work."""
        Config._CONFIG = minimal_config
        Config._loaded = True
        Config._logger = logging.getLogger("wiz_sdk")
        assert Config._loaded is True

    def test_get_nested_key(self, mock_config):
        assert mock_config.get("api", "max_retries") == 2
        assert mock_config.get("api", "timeout") == 5

    def test_get_missing_key_returns_default(self, mock_config):
        assert mock_config.get("nonexistent", "key", default="fallback") == "fallback"

    def test_get_deeply_nested(self, mock_config):
        assert mock_config.get("auth", "credentials", "storage_method") == "env"

    def test_get_empty_path(self, mock_config):
        # Calling get with no keys returns the whole config
        result = mock_config.get(default=None)
        assert result is not None


class TestConfigAccessors:
    def test_default_domain(self, mock_config):
        result = Config.get("domain", "default", default="gov")
        assert result == "gov"

    def test_api_timeout(self, mock_config):
        assert Config.api_timeout() == 5

    def test_api_timeout_default(self, minimal_config):
        """When api.timeout is not set, should return default."""
        del minimal_config["api"]["timeout"]
        Config._CONFIG = minimal_config
        Config._loaded = True
        Config._logger = logging.getLogger("wiz_sdk")
        assert Config.api_timeout() == 180

    def test_validate_queries_default_false(self, mock_config):
        assert Config.validate_queries() is False

    def test_validate_queries_enabled(self, minimal_config):
        minimal_config["api"]["validate_queries"] = True
        Config._CONFIG = minimal_config
        Config._loaded = True
        Config._logger = logging.getLogger("wiz_sdk")
        assert Config.validate_queries() is True

    def test_serverless_default_false(self, mock_config):
        assert Config.serverless() is False

    def test_report_stream_by_default(self, mock_config):
        assert Config.report_stream_by_default() is True

    def test_report_export_type(self, mock_config):
        assert Config.report_export_type() == "json"


class TestSetLogLevel:
    def test_set_log_level_string(self, mock_config):
        level = Config.set_log_level("DEBUG")
        assert level == logging.DEBUG

    def test_set_log_level_int(self, mock_config):
        level = Config.set_log_level(20)
        assert level == logging.INFO

    def test_set_log_level_with_handler_level(self, mock_config):
        level = Config.set_log_level("DEBUG", handler_level="WARNING")
        assert level == logging.DEBUG


class TestGenerateDefaultConfig:
    def test_generates_config_file(self, tmp_path):
        config_path = tmp_path / "wiz.config"
        with patch("wiz_sdk.config.DEFAULT_WIZ_DIR", tmp_path):
            generate_default_config(file_path=config_path)

        assert config_path.exists()
        content = config_path.read_text()
        assert "app:" in content
        assert "name:" in content

    def test_creates_credentials_file(self, tmp_path):
        config_path = tmp_path / "wiz.config"
        creds_path = tmp_path / "wiz.credentials"
        with patch("wiz_sdk.config.DEFAULT_WIZ_DIR", tmp_path):
            generate_default_config(file_path=config_path)

        assert creds_path.exists()

    def test_skips_in_serverless(self):
        with patch("wiz_sdk.config.SERVERLESS", True):
            result = generate_default_config()
        assert result is False

    def test_config_is_valid_yaml(self, tmp_path):
        config_path = tmp_path / "wiz.config"
        with patch("wiz_sdk.config.DEFAULT_WIZ_DIR", tmp_path):
            generate_default_config(file_path=config_path)

        content = config_path.read_text()
        parsed = yaml.safe_load(content)
        assert parsed["app"]["name"] is not None


class TestGetLogger:
    def test_returns_logger(self, mock_config):
        logger = Config.get_logger()
        assert isinstance(logger, logging.Logger)

    def test_child_logger_auto_names_from_caller(self, mock_config):
        """get_logger() auto-detects the calling module name."""
        logger = Config.get_logger()
        # Should contain the test file name as part of the child logger name
        assert "test_config" in logger.name
