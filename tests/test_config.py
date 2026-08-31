"""Tests for config.py — Config singleton and accessors."""

import logging
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from wizsec.config import Config, generate_default_config, DEFAULT_WIZ_DIR
from wizsec.exceptions import WizConfigurationError


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

    @pytest.mark.parametrize(
        ("value", "default"),
        [
            (False, True),
            (0, 99),
            ("", "fallback"),
            ({}, {"fallback": True}),
            ([], ["fallback"]),
        ],
    )
    def test_get_preserves_falsy_config_values(self, mock_config, value, default):
        Config.set("falsy", "value", value=value)
        assert Config.get("falsy", "value", default=default) == value

    def test_get_deeply_nested(self, mock_config):
        assert mock_config.get("auth", "credentials", "storage_method") == "env"

    def test_get_empty_path(self, mock_config):
        # Calling get with no keys returns the whole config
        result = mock_config.get(default=None)
        assert result is not None


class TestConfigSet:
    def test_set_then_get_round_trip(self, mock_config):
        Config.set("api", "timeout", value=99)
        assert Config.get("api", "timeout") == 99

    def test_set_creates_missing_intermediates(self, mock_config):
        Config.set("new_section", "nested", "leaf", value="hello")
        assert Config.get("new_section", "nested", "leaf") == "hello"

    def test_set_overwrites_non_dict_intermediate(self, mock_config):
        Config.set("api", "timeout", value=10)  # leaf is currently an int
        Config.set("api", "timeout", "nested", value="ok")
        assert Config.get("api", "timeout", "nested") == "ok"

    def test_set_with_no_keys_raises(self, mock_config):
        with pytest.raises(ValueError):
            Config.set(value="x")

    def test_set_saved_data_directory_reflected(self, mock_config, tmp_path):
        new_dir = tmp_path / "alt-saved"
        Config.set("saved_data", "directory", value=str(new_dir))
        assert Config.saved_data_directory() == new_dir


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
            "app": {"name": "test-app", "release": "2.0.0", "config_schema": 1},
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
        ):
            Config.load(config_path=str(config_file))

        assert Config._loaded is True
        assert Config._CONFIG["app"]["name"] == "test-app"

    def test_load_applies_overrides(self, tmp_path):
        """load() should apply dot-notation overrides on top of YAML values."""
        config_data = {
            "app": {"name": "original", "release": "1.0.0", "config_schema": 1},
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
        ):
            Config.load(config_path=str(config_file), overrides=overrides)

        assert Config._CONFIG["api"]["timeout"] == 120
        assert Config._CONFIG["app"]["name"] == "overridden"

    def test_load_sets_env_vars_for_credentials(self, tmp_path):
        """load() should set WIZ_CLIENT_ID / WIZ_CLIENT_SECRET env vars when provided."""
        config_data = {
            "app": {"name": "t", "release": "0", "config_schema": 1},
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
            "app": {"name": "t", "release": "0", "config_schema": 1},
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
        ):
            Config.load(config_path=str(config_file), overrides=overrides)

        # Should load successfully despite bad overrides
        assert Config._loaded is True


# ---------------------------------------------------------------------------
# Config schema migration
# ---------------------------------------------------------------------------


class TestConfigMigration:
    def _write_config(self, tmp_path, extra_app_fields=None):
        config_data = {
            "app": {"name": "wizsec", "release": "1.0.0", **(extra_app_fields or {})},
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
        return config_file

    def test_migration_adds_schema_field_when_missing(self, tmp_path):
        """Config without config_schema should have it added after load."""
        config_file = self._write_config(tmp_path)

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        assert Config._CONFIG["app"]["config_schema"] == 2

    def test_migration_updates_file_when_schema_missing(self, tmp_path):
        """_run_migrations should write config_schema into the config file."""
        config_file = self._write_config(tmp_path)
        assert "config_schema" not in config_file.read_text()

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        assert "config_schema: 2" in config_file.read_text()

    def test_migration_skips_when_already_current(self, tmp_path):
        """Config at current schema version should not trigger migration."""
        config_file = self._write_config(tmp_path, {"config_schema": 2})

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        # Schema field is still current and file was not re-written needlessly
        assert Config._CONFIG["app"]["config_schema"] == 2

    def test_migration_runs_registered_function(self, tmp_path):
        """A registered migration function should be called and its changes reported."""
        from wizsec.config import _MIGRATIONS

        def fake_migration(config):
            config.setdefault("api", {})["new_field"] = "default_value"
            return ["Added: 'api.new_field' (default: default_value)"]

        config_file = self._write_config(tmp_path)

        try:
            _MIGRATIONS[(0, 1)] = fake_migration
            with (
                patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
                patch("wizsec.config.SERVERLESS", False),
            ):
                Config.load(config_path=str(config_file))

            assert Config._CONFIG["api"]["new_field"] == "default_value"
            assert Config._CONFIG["app"]["config_schema"] == 2
            assert Config._CONFIG["api"]["auto_paginate"] is True
        finally:
            _MIGRATIONS.pop((0, 1), None)

    def test_migration_prints_changes_to_stdout(self, tmp_path, capsys):
        """Migration with changes should print an info message to stdout."""
        from wizsec.config import _MIGRATIONS

        def fake_migration(config):
            return ["Added: 'app.timeout' (default: 30)"]

        config_file = self._write_config(tmp_path)

        try:
            _MIGRATIONS[(0, 1)] = fake_migration
            with (
                patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
                patch("wizsec.config.SERVERLESS", False),
            ):
                Config.load(config_path=str(config_file))

            captured = capsys.readouterr()
            assert "Info: Config migrated to schema v2" in captured.out
            assert "Added: 'app.timeout'" in captured.out
        finally:
            _MIGRATIONS.pop((0, 1), None)

    def test_migration_no_output_when_no_changes(self, tmp_path, capsys):
        """Current-schema config should produce no migration stdout."""
        config_file = self._write_config(tmp_path, {"config_schema": 2})

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        captured = capsys.readouterr()
        assert "migrated" not in captured.out

    def test_v2_migration_preserves_existing_values(self, tmp_path):
        config_file = self._write_config(tmp_path)
        config_data = yaml.safe_load(config_file.read_text())
        config_data["api"]["auto_paginate"] = False
        config_data["domain"]["app"] = {"enabled": True}
        config_file.write_text(yaml.dump(config_data))

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        assert Config._CONFIG["api"]["auto_paginate"] is False
        assert Config._CONFIG["domain"]["app"]["enabled"] is True

    def test_v2_file_update_preserves_comments(self, tmp_path):
        config_file = tmp_path / "wiz.config"
        config_file.write_text(
            "\n".join(
                [
                    "app:",
                    "  name: wizsec",
                    "  release: 1.0.0",
                    "  # custom user comment",
                    "api:",
                    "  timeout: 5",
                    "",
                ]
            )
        )

        with (
            patch("wizsec.config.DEFAULT_WIZ_DIR", tmp_path),
            patch("wizsec.config.SERVERLESS", False),
        ):
            Config.load(config_path=str(config_file))

        content = config_file.read_text()
        assert "# custom user comment" in content
        assert "config_schema: 2" in content
        assert "auto_paginate: true" in content

    def test_migration_file_write_failure_does_not_raise(self, tmp_path):
        """If the config file cannot be written, migration should not raise."""
        from wizsec import config as cfg

        config_without_schema = {"app": {"name": "wizsec", "release": "1.0.0"}}
        nonexistent = tmp_path / "no" / "such" / "wiz.config"

        # Should not raise even though the file path is invalid
        cfg._run_migrations(config_without_schema, nonexistent)

        # In-memory dict is still updated
        assert config_without_schema["app"]["config_schema"] == 2


# ---------------------------------------------------------------------------
# Config.ensure_loaded() decorator
# ---------------------------------------------------------------------------


class TestEnsureLoaded:
    def test_ensure_loaded_calls_load_when_not_loaded(self, tmp_path):
        """ensure_loaded decorator should call load() when _loaded is False."""
        config_data = {
            "app": {"name": "auto", "release": "0.1", "config_schema": 1},
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
        ):
            # Calling an accessor should auto-load
            result = Config.default_domain()

        assert Config._loaded is True
        assert result == "gov"

    def test_ensure_loaded_skips_load_when_already_loaded(self, mock_config):
        """ensure_loaded should not call load() again when already loaded."""
        with patch.object(Config, "load") as mock_load:
            Config.default_domain()
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

    def test_get_proxies_blank_config_falls_back_to_env(self, mock_config):
        mock_config._CONFIG["auth"]["proxy"] = {
            "http": {"url": "", "port": "8080"},
            "https": {"url": "", "port": "8443"},
        }
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://env-proxy:8080",
                "HTTPS_PROXY": "https://env-proxy:8443",
            },
        ):
            result = Config.get_proxies()
        assert result["http"] == "http://env-proxy:8080"
        assert result["https"] == "https://env-proxy:8443"

    def test_domain_enabled_false_by_default(self, mock_config):
        assert Config.domain_enabled("fedramp") is False

    def test_domain_enabled_when_set(self, mock_config):
        mock_config._CONFIG["domain"]["app"] = {"enabled": True}
        assert Config.domain_enabled("app") is True

    def test_domain_root_known_envs(self, mock_config):
        assert Config.domain_root("app") == "app.wiz.io"
        assert Config.domain_root("gov") == "gov.wiz.io"
        assert Config.domain_root("fedramp") == "app.wiz.us"

    def test_domain_root_unknown_returns_none(self, mock_config):
        result = Config.domain_root("unknown")
        assert result is None

    def test_validate_domain_rejects_unknown(self, mock_config):
        with pytest.raises(WizConfigurationError):
            Config.validate_domain("unknown")

    def test_validate_domain_rejects_disabled(self, mock_config):
        with pytest.raises(WizConfigurationError):
            Config.validate_domain("fedramp")

    def test_api_auto_paginate(self, mock_config):
        assert Config.api_auto_paginate() is True

    def test_api_max_retries(self, mock_config):
        assert Config.api_max_retries() == 2

    def test_api_retry(self, mock_config):
        assert Config.api_retry() == 0.01

    def test_report_retry_time_default(self, mock_config):
        assert Config.report_retry_time() == 30

    def test_report_max_retries_default(self, mock_config):
        assert Config.report_max_retries() == 3

    def test_report_polling_time_default(self, mock_config):
        assert Config.report_polling_time() == 15

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


class TestRateLimitConfig:
    def test_headroom_default(self, mock_config):
        assert Config.rate_limit_headroom() == 0.8

    def test_headroom_from_config(self, mock_config):
        Config._CONFIG["rate_limit"] = {"headroom": 0.5}
        assert Config.rate_limit_headroom() == 0.5

    def test_headroom_invalid_falls_back(self, mock_config):
        Config._CONFIG["rate_limit"] = {"headroom": "not-a-number"}
        assert Config.rate_limit_headroom() == 0.8

    def test_headroom_nonpositive_falls_back(self, mock_config):
        Config._CONFIG["rate_limit"] = {"headroom": 0}
        assert Config.rate_limit_headroom() == 0.8

    def test_headroom_above_one_honored(self, mock_config):
        Config._CONFIG["rate_limit"] = {"headroom": 1.5}
        assert Config.rate_limit_headroom() == 1.5

    def test_overrides_default_empty(self, mock_config):
        assert Config.rate_limit_overrides() == {}

    def test_overrides_filters_invalid_entries(self, mock_config):
        Config._CONFIG["rate_limit"] = {
            "overrides": {
                "query_service": 8,
                "mutation_user": "bad",
                "query_user": -1,
                "mutation_service": 2.4,
            }
        }
        assert Config.rate_limit_overrides() == {
            "query_service": 8.0,
            "mutation_service": 2.4,
        }

    def test_overrides_non_dict_ignored(self, mock_config):
        Config._CONFIG["rate_limit"] = {"overrides": "nope"}
        assert Config.rate_limit_overrides() == {}

    def test_max_backoff_waits_default(self, mock_config):
        assert Config.rate_limit_max_backoff_waits() == 10

    def test_max_backoff_waits_from_config(self, mock_config):
        Config._CONFIG["rate_limit"] = {"max_backoff_waits": 4}
        assert Config.rate_limit_max_backoff_waits() == 4

    def test_max_backoff_waits_invalid_falls_back(self, mock_config):
        Config._CONFIG["rate_limit"] = {"max_backoff_waits": "nope"}
        assert Config.rate_limit_max_backoff_waits() == 10

    def test_default_retry_after_default(self, mock_config):
        assert Config.rate_limit_default_retry_after() == 10

    def test_default_retry_after_from_config(self, mock_config):
        Config._CONFIG["rate_limit"] = {"default_retry_after": 30}
        assert Config.rate_limit_default_retry_after() == 30

    def test_default_retry_after_nonpositive_falls_back(self, mock_config):
        Config._CONFIG["rate_limit"] = {"default_retry_after": 0}
        assert Config.rate_limit_default_retry_after() == 10


class TestNumericSettingCoercion:
    """Numeric settings arrive from YAML as whatever the user typed. A quoted
    value used to survive the getter as a str and only blow up later, inside
    time.sleep() or an arithmetic deadline, with a TypeError naming neither
    the setting nor the file."""

    QUOTED = [
        ("api", "timeout", "45", lambda: Config.api_timeout(), 45.0),
        ("api", "retry_time", "3", lambda: Config.api_retry(), 3.0),
        ("api", "max_retries", "7", lambda: Config.api_max_retries(), 7),
        ("reports", "timeout", "1800", lambda: Config.report_timeout(), 1800.0),
        ("reports", "retry_time", "20", lambda: Config.report_retry_time(), 20.0),
        ("reports", "polling_time", "5", lambda: Config.report_polling_time(), 5.0),
        ("reports", "max_retries", "9", lambda: Config.report_max_retries(), 9),
    ]

    @pytest.mark.parametrize("section,key,raw,getter,expected", QUOTED)
    def test_quoted_numbers_coerce(
        self, mock_config, section, key, raw, getter, expected
    ):
        Config._CONFIG[section] = {key: raw}
        assert getter() == expected

    @pytest.mark.parametrize("section,key,raw,getter,expected", QUOTED)
    def test_garbage_falls_back_instead_of_raising(
        self, mock_config, section, key, raw, getter, expected
    ):
        Config._CONFIG[section] = {key: "not-a-number"}
        assert isinstance(getter(), (int, float))

    @pytest.mark.parametrize("section,key,raw,getter,expected", QUOTED)
    def test_negative_falls_back(
        self, mock_config, section, key, raw, getter, expected
    ):
        Config._CONFIG[section] = {key: -1}
        result = getter()
        assert result > 0

    def test_durations_keep_fractional_seconds(self, mock_config):
        """time.sleep() and requests timeouts take floats, and sub-second
        values are how the suite keeps retry paths fast. Truncating to int
        would turn a 0.01s backoff into no backoff at all."""
        Config._CONFIG["api"] = {"retry_time": 0.01, "timeout": 2.5}
        assert Config.api_retry() == 0.01
        assert Config.api_timeout() == 2.5

    def test_zero_allowed_where_meaningful(self, mock_config):
        """Zero retries and zero backoff are real choices, not misconfiguration."""
        Config._CONFIG["api"] = {"max_retries": 0, "retry_time": 0}
        assert Config.api_max_retries() == 0
        assert Config.api_retry() == 0.0

    def test_zero_rejected_where_it_would_break(self, mock_config):
        """A 0s poll interval spins against the API; a 0s timeout expires
        before the first attempt."""
        Config._CONFIG["reports"] = {"polling_time": 0, "timeout": 0}
        assert Config.report_polling_time() == 15.0
        assert Config.report_timeout() == 3600.0

    def test_counts_stay_ints(self, mock_config):
        Config._CONFIG["api"] = {"max_retries": "4"}
        assert isinstance(Config.api_max_retries(), int)

    def test_query_splitting_invalid_falls_back(self, mock_config):
        """These already coerced with a bare int(), which raised ValueError on
        bad input rather than falling back like every other setting."""
        Config._CONFIG["query_splitting"] = {"threshold": "lots", "max_concurrent": 0}
        assert Config.query_splitting_threshold() == 10000
        assert Config.query_splitting_max_concurrent() == 10


class TestBooleanSettingCoercion:
    """Every non-empty string is truthy, so a quoted `enabled: "false"` used to
    read as True and silently enable what the user wrote the line to turn off."""

    def test_quoted_false_disables_pickle(self, mock_config):
        """The consequential one: this gates pickle deserialization, so reading
        a quoted "false" as True enabled an RCE vector for someone who had
        written the setting specifically to disable it."""
        Config._CONFIG["saved_data"] = {"pickle": "false"}
        assert Config.saved_data_pickle_enabled() is False

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "no", "off", "n", "0"])
    def test_falsy_tokens(self, mock_config, raw):
        Config._CONFIG["api"] = {"auto_paginate": raw}
        assert Config.api_auto_paginate() is False

    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "y", "1"])
    def test_truthy_tokens(self, mock_config, raw):
        Config._CONFIG["api"] = {"validate_queries": raw}
        assert Config.validate_queries() is True

    def test_surrounding_whitespace_is_ignored(self, mock_config):
        Config._CONFIG["api"] = {"auto_paginate": "  OFF  "}
        assert Config.api_auto_paginate() is False

    def test_real_booleans_pass_through(self, mock_config):
        Config._CONFIG["api"] = {"auto_paginate": False, "validate_queries": True}
        assert Config.api_auto_paginate() is False
        assert Config.validate_queries() is True

    def test_numbers_follow_zero_nonzero(self, mock_config):
        Config._CONFIG["api"] = {"auto_paginate": 0, "validate_queries": 1}
        assert Config.api_auto_paginate() is False
        assert Config.validate_queries() is True

    @pytest.mark.parametrize(
        "raw",
        [
            "maybe",
            "",
            None,
            [],
            {},
        ],
    )
    def test_unrecognised_values_fall_back_to_default(self, mock_config, raw):
        """A typo must not flip a flag on via Python truthiness — it falls back
        to the documented default, in both directions."""
        Config._CONFIG["api"] = {"auto_paginate": raw, "validate_queries": raw}
        assert Config.api_auto_paginate() is True  # default True
        assert Config.validate_queries() is False  # default False

    def test_domain_enabled_coerces(self, mock_config):
        Config._CONFIG["domain"] = {"gov": {"enabled": "false"}}
        assert Config.domain_enabled("gov") is False

    def test_getters_return_real_bools(self, mock_config):
        """Callers and `is True` comparisons should never see a str or int."""
        Config._CONFIG["logging"] = {"enabled": "yes", "verbose": "0"}
        assert Config.logging_enabled() is True
        assert Config.verbose_mode() is False
