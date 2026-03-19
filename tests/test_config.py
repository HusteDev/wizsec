"""Tests for config.py — Config singleton and accessors."""

import logging
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from wizsec.config import Config, generate_default_config, DEFAULT_WIZ_DIR


class TestConfigLoading:
    def test_load_from_dict(self, minimal_config):
        """Loading a config dict directly should work."""
        Config._CONFIG = minimal_config
        Config._loaded = True
        Config._logger = logging.getLogger("wizsec")
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
        Config._logger = logging.getLogger("wizsec")
        assert Config.api_timeout() == 180

    def test_validate_queries_default_false(self, mock_config):
        assert Config.validate_queries() is False

    def test_validate_queries_enabled(self, minimal_config):
        minimal_config["api"]["validate_queries"] = True
        Config._CONFIG = minimal_config
        Config._loaded = True
        Config._logger = logging.getLogger("wizsec")
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
        with patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path):
            generate_default_config(file_path=config_path)

        assert config_path.exists()
        content = config_path.read_text()
        assert "app:" in content
        assert "name:" in content

    def test_creates_credentials_file(self, tmp_path):
        config_path = tmp_path / "wiz.config"
        creds_path = tmp_path / "wiz.credentials"
        with patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path):
            generate_default_config(file_path=config_path)

        assert creds_path.exists()

    def test_skips_in_serverless(self):
        with patch("wizsec.config.SERVERLESS", True):
            result = generate_default_config()
        assert result is False

    def test_config_is_valid_yaml(self, tmp_path):
        config_path = tmp_path / "wiz.config"
        with patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path):
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


# ---------------------------------------------------------------------------
# Config.load() — loading from a YAML file
# ---------------------------------------------------------------------------


class TestConfigLoad:
    def test_load_from_yaml_file(self, tmp_path):
        """load() should read a YAML config file and populate _CONFIG."""
        config_data = {
            "app": {"name": "test-app", "release": "2.0.0"},
            "auth": {
                "grant_type": "client_credentials",
                "credentials": {"storage_method": "env"},
            },
            "domain": {"default": "gov"},
            "api": {"max_retries": 3, "retry_time": 1, "timeout": 60},
            "logging": {"enabled": False},
        }
        config_file = tmp_path / "wiz.config"
        config_file.write_text(yaml.dump(config_data))

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
            patch("wizsec.config.get_version", side_effect=Exception("not installed")),
        ):
            Config.load(config_path=str(config_file))

        assert Config._loaded is True
        assert Config._CONFIG["app"]["name"] == "test-app"

    def test_load_applies_overrides(self, tmp_path):
        """load() should apply dot-notation overrides on top of YAML values."""
        config_data = {
            "app": {"name": "original", "release": "1.0.0"},
            "auth": {
                "grant_type": "client_credentials",
                "credentials": {"storage_method": "env"},
            },
            "domain": {"default": "gov"},
            "api": {"max_retries": 2, "retry_time": 1, "timeout": 30},
            "logging": {"enabled": False},
        }
        config_file = tmp_path / "wiz.config"
        config_file.write_text(yaml.dump(config_data))

        overrides = ["api.timeout=120", "app.name=overridden"]
        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
            patch("wizsec.config.get_version", side_effect=Exception("not installed")),
        ):
            Config.load(config_path=str(config_file), overrides=overrides)

        assert Config._CONFIG["api"]["timeout"] == 120
        assert Config._CONFIG["app"]["name"] == "overridden"

    def test_load_sets_env_vars_for_credentials(self, tmp_path):
        """load() should set WIZ_CLIENT_ID / WIZ_CLIENT_SECRET env vars when provided."""
        config_data = {
            "app": {"name": "t", "release": "0"},
            "auth": {
                "grant_type": "client_credentials",
                "credentials": {"storage_method": "env"},
            },
            "domain": {"default": "gov"},
            "api": {"max_retries": 1, "retry_time": 0, "timeout": 5},
            "logging": {"enabled": False},
        }
        config_file = tmp_path / "wiz.config"
        config_file.write_text(yaml.dump(config_data))

        try:
            with (
                patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
                patch("wizsec.config.SERVERLESS", False),
                patch("wizsec.config.get_version", side_effect=Exception("skip")),
            ):
                Config.load(
                    config_path=str(config_file),
                    client_id="test-id",
                    client_secret="test-secret",
                )

            assert os.environ.get("WIZ_CLIENT_ID") == "test-id"
            assert os.environ.get("WIZ_CLIENT_SECRET") == "test-secret"
        finally:
            os.environ.pop("WIZ_CLIENT_ID", None)
            os.environ.pop("WIZ_CLIENT_SECRET", None)

    def test_load_skips_overrides_without_equals(self, tmp_path):
        """Overrides without '=' should be silently skipped."""
        config_data = {
            "app": {"name": "t", "release": "0"},
            "auth": {
                "grant_type": "client_credentials",
                "credentials": {"storage_method": "env"},
            },
            "domain": {"default": "gov"},
            "api": {"max_retries": 1, "retry_time": 0, "timeout": 5},
            "logging": {"enabled": False},
        }
        config_file = tmp_path / "wiz.config"
        config_file.write_text(yaml.dump(config_data))

        overrides = ["no-equals-here", 42]  # invalid overrides
        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
            patch("wizsec.config.get_version", side_effect=Exception("skip")),
        ):
            Config.load(config_path=str(config_file), overrides=overrides)

        # Should load successfully despite bad overrides
        assert Config._loaded is True


# ---------------------------------------------------------------------------
# Config.ensure_loaded() decorator
# ---------------------------------------------------------------------------


class TestEnsureLoaded:
    def test_ensure_loaded_calls_load_when_not_loaded(self, tmp_path):
        """ensure_loaded decorator should call load() when _loaded is False."""
        config_data = {
            "app": {"name": "auto", "release": "0.1"},
            "auth": {
                "grant_type": "client_credentials",
                "credentials": {"storage_method": "env"},
            },
            "domain": {"default": "gov"},
            "api": {"max_retries": 1, "retry_time": 0, "timeout": 10},
            "logging": {"enabled": False},
        }
        config_file = tmp_path / "wiz.config"
        config_file.write_text(yaml.dump(config_data))

        assert Config._loaded is False

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
            patch("wizsec.config.get_version", side_effect=Exception("skip")),
        ):
            # Calling an accessor should auto-load
            result = Config.app_name()

        assert Config._loaded is True
        assert result == "auto"

    def test_ensure_loaded_skips_load_when_already_loaded(self, mock_config):
        """ensure_loaded should not call load() again when already loaded."""
        with patch.object(Config, "load") as mock_load:
            Config.app_name()
            mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# Config.load_dotenv()
# ---------------------------------------------------------------------------


class TestLoadDotenv:
    def test_load_dotenv_calls_dotenv(self, mock_config, tmp_path):
        """load_dotenv should invoke python-dotenv when .env exists."""
        mock_config._CONFIG["app"] = {"name": "wizsec", "env_path": str(tmp_path)}
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR=hello")

        with patch("dotenv.load_dotenv") as mock_ld:
            Config.load_dotenv()
            mock_ld.assert_called_once()

    def test_load_dotenv_skips_in_serverless(self, mock_config):
        """load_dotenv should return early in serverless mode."""
        with patch("wizsec.config.SERVERLESS", True):
            # Should return before importing dotenv at all
            Config.load_dotenv()


# ---------------------------------------------------------------------------
# Additional Accessor Tests
# ---------------------------------------------------------------------------


class TestConfigAccessorsExtended:
    def test_app_name(self, mock_config):
        assert Config.app_name() == "wizsec"

    def test_release_version(self, mock_config):
        assert Config.release_version() == "1.0.0"

    def test_wiz_dir_returns_path(self, mock_config, tmp_path):
        mock_config._CONFIG["app"] = {"name": "wizsec", "wiz_dir": str(tmp_path)}
        result = Config.wiz_dir()
        assert isinstance(result, Path)
        assert result == tmp_path

    def test_env_path_returns_path(self, mock_config, tmp_path):
        mock_config._CONFIG["app"]["env_path"] = str(tmp_path)
        result = Config.env_path()
        assert isinstance(result, Path)

    def test_saved_data_directory(self, mock_config, tmp_path):
        mock_config._CONFIG["saved_data"]["directory"] = str(tmp_path / "data")
        result = Config.saved_data_directory()
        assert isinstance(result, Path)

    def test_saved_data_temp_directory(self, mock_config, tmp_path):
        mock_config._CONFIG["saved_data"]["temp"] = str(tmp_path / "temp")
        result = Config.saved_data_temp_directory()
        assert isinstance(result, Path)

    def test_grant_type(self, mock_config):
        assert Config.grant_type() == "client_credentials"

    def test_storage_method(self, mock_config):
        assert Config.storage_method() == "env"

    def test_credential_file_path(self, mock_config, tmp_path):
        mock_config._CONFIG["auth"]["credentials"]["file_path"] = str(tmp_path)
        result = Config.credential_file_path()
        assert isinstance(result, Path)

    def test_ca_cert_default_empty(self, mock_config):
        assert Config.ca_cert() == ""

    def test_quiet_auth_default(self, mock_config):
        assert Config.quiet_auth() == "true"

    def test_auth_poll_time_default(self, mock_config):
        assert Config.auth_poll_time() == 5

    def test_get_proxies_returns_dict(self, mock_config):
        result = Config.get_proxies()
        assert isinstance(result, dict)
        assert "http" in result
        assert "https" in result

    def test_get_proxies_with_config(self, mock_config):
        mock_config._CONFIG["auth"]["proxy"] = {
            "http": {"url": "http://proxy", "port": "8080"},
            "https": {"url": "https://proxy", "port": "8443"},
        }
        result = Config.get_proxies()
        assert "http://proxy:8080" == result["http"]
        assert "https://proxy:8443" == result["https"]

    def test_domain_enabled_false_by_default(self, mock_config):
        assert Config.domain_enabled("app") is False

    def test_domain_enabled_when_set(self, mock_config):
        mock_config._CONFIG["domain"]["app"] = {"enabled": True}
        assert Config.domain_enabled("app") is True

    def test_domain_root_known_envs(self, mock_config):
        assert Config.domain_root("app") == "app.wiz.io"
        assert Config.domain_root("gov") == "gov.wiz.io"
        assert Config.domain_root("fedramp") == "app.wiz.us"

    def test_domain_root_unknown_falls_back(self, mock_config):
        # Unknown env should fall back to default_domain ("gov")
        result = Config.domain_root("unknown")
        assert result == "gov.wiz.io"

    def test_api_max_retries(self, mock_config):
        assert Config.api_max_retries() == 2

    def test_api_retry(self, mock_config):
        assert Config.api_retry() == 0.01

    def test_report_export_directory(self, mock_config, tmp_path):
        mock_config._CONFIG["reports"] = {"export_directory": str(tmp_path)}
        result = Config.report_export_directory()
        assert isinstance(result, Path)

    def test_report_retry_time_default(self, mock_config):
        assert Config.report_retry_time() == 30

    def test_report_max_retries_default(self, mock_config):
        assert Config.report_max_retries() == 3

    def test_report_polling_time_default(self, mock_config):
        assert Config.report_polling_time() == 15

    def test_report_auto_cleanup_default(self, mock_config):
        assert Config.report_auto_cleanup() is False

    def test_report_save_incomplete_default(self, mock_config):
        assert Config.report_save_incomplete() is True

    def test_logging_enabled_when_set_true(self, mock_config):
        mock_config._CONFIG["logging"]["enabled"] = True
        assert Config.logging_enabled() is True

    def test_logger_min_level_default(self, mock_config):
        assert Config.logger_min_level() == 10

    def test_verbose_mode_default(self, mock_config):
        assert Config.verbose_mode() is False

    def test_debug_mode_default(self, mock_config):
        assert Config.debug_mode() is False

    def test_file_logging_enabled_default(self, mock_config):
        assert Config.file_logging_enabled() is False

    def test_file_handler_logging_level_default(self, mock_config):
        assert Config.file_handler_logging_level() == "DEBUG"

    def test_file_handler_log_directory_default(self, mock_config):
        result = Config.file_handler_log_directory()
        # With empty string default, should return DEFAULT_WIZ_DIR
        assert result is not None

    def test_file_handler_create_log_dir_default(self, mock_config):
        assert Config.file_handler_create_log_dir() is True

    def test_file_handler_markdown_enabled_default(self, mock_config):
        assert Config.file_handler_markdown_enabled() is False

    def test_console_handler_enabled_when_set(self, mock_config):
        mock_config._CONFIG["logging"]["console_handler"] = {"enabled": True}
        assert Config.console_handler_enabled() is True

    def test_console_handler_logging_level_default(self, mock_config):
        assert Config.console_handler_logging_level() == "INFO"

    def test_saved_data_enabled(self, mock_config):
        assert Config.saved_data_enabled() is True

    def test_saved_data_pickle_enabled_default(self, mock_config):
        assert Config.saved_data_pickle_enabled() is False

    def test_default_domain(self, mock_config):
        assert Config.default_domain() == "gov"


# ---------------------------------------------------------------------------
# expand_all_vars()
# ---------------------------------------------------------------------------


class TestExpandAllVars:
    def test_expands_home(self):
        from wizsec.config import expand_all_vars

        result = expand_all_vars("$HOME/test")
        assert str(Path.home()) in result
        assert result.endswith("/test") or result.endswith("\\test")

    def test_expands_cwd(self):
        from wizsec.config import expand_all_vars

        result = expand_all_vars("$CWD/output")
        assert str(Path.cwd()) in result

    def test_expands_tilde(self):
        from wizsec.config import expand_all_vars

        result = expand_all_vars("~/somedir")
        assert "~" not in result

    def test_expands_env_var(self):
        from wizsec.config import expand_all_vars

        with patch.dict(os.environ, {"MY_TEST_VAR": "/custom/path"}):
            result = expand_all_vars("$MY_TEST_VAR/sub")
        assert "/custom/path" in result


# ---------------------------------------------------------------------------
# parse_filepath()
# ---------------------------------------------------------------------------


class TestParseFilepath:
    def test_none_returns_cwd(self):
        from wizsec.config import parse_filepath

        directory, filename = parse_filepath(None)
        assert directory == Path.cwd()
        assert filename is None

    def test_file_path_returns_parent_and_name(self, tmp_path):
        from wizsec.config import parse_filepath

        test_file = tmp_path / "data.json"
        test_file.touch()
        directory, filename = parse_filepath(str(test_file))
        assert directory == tmp_path
        assert filename == "data.json"

    def test_directory_path_returns_dir_and_none(self, tmp_path):
        from wizsec.config import parse_filepath

        directory, filename = parse_filepath(str(tmp_path))
        assert directory == tmp_path
        assert filename is None

    def test_relative_path_resolved_to_absolute(self):
        from wizsec.config import parse_filepath

        directory, filename = parse_filepath("relative/dir")
        assert directory.is_absolute()
