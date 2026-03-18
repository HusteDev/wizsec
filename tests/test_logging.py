"""Tests for _logging module."""

import logging
from unittest.mock import MagicMock

import pytest

from wiz_sdk._logging import (
    BASE_LOGGER_NAME,
    VERBOSE_LEVEL,
    CustomFormatter,
    MarkdownFormatter,
    _register_verbose_level,
    logging_init,
    parse_level,
)


class TestVerboseLevel:
    def test_verbose_level_value(self):
        assert VERBOSE_LEVEL == 15

    def test_verbose_registered(self):
        _register_verbose_level()
        assert hasattr(logging, "VERBOSE")
        assert logging.VERBOSE == 15
        assert logging.getLevelName(15) == "VERBOSE"

    def test_verbose_method_on_logger(self):
        _register_verbose_level()
        logger = logging.getLogger("test_verbose")
        assert hasattr(logger, "verbose")


class TestParseLevel:
    def test_none_returns_default(self):
        assert parse_level(None) == logging.INFO
        assert parse_level(None, default=logging.WARNING) == logging.WARNING

    def test_int_passthrough(self):
        assert parse_level(10) == 10
        assert parse_level(20) == 20

    def test_string_name(self):
        assert parse_level("DEBUG") == logging.DEBUG
        assert parse_level("warning") == logging.WARNING

    def test_string_digit(self):
        assert parse_level("15") == 15

    def test_verbose_string(self):
        _register_verbose_level()
        assert parse_level("VERBOSE") == 15

    def test_invalid_string_returns_default(self):
        assert parse_level("NOT_A_LEVEL") == logging.INFO


class TestCustomFormatter:
    def test_format_time_with_datefmt(self):
        formatter = CustomFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        result = formatter.formatTime(record, "%Y")
        assert len(result) == 4  # just a year

    def test_format_time_default(self):
        formatter = CustomFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        result = formatter.formatTime(record)
        assert "." in result  # microseconds included


class TestMarkdownFormatter:
    def test_first_format_includes_header(self):
        MarkdownFormatter.header_written = False
        fmt = MarkdownFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        result = fmt.format(record)
        assert "| Timestamp |" in result
        assert "hello" in result

    def test_subsequent_format_no_header(self):
        MarkdownFormatter.header_written = True
        fmt = MarkdownFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "world", (), None)
        result = fmt.format(record)
        assert "| Timestamp |" not in result
        assert "world" in result


class TestLoggingInit:
    @pytest.fixture(autouse=True)
    def _reset_base_logger(self):
        """Ensure a clean base logger for each test."""
        base = logging.getLogger(BASE_LOGGER_NAME)
        base.handlers.clear()
        base.propagate = False  # prevent pytest root handlers from interfering
        base._baselogger_initialized = False
        yield
        base.handlers.clear()
        base._baselogger_initialized = False

    def _make_mock_config(self, logging_enabled=False, serverless=False):
        config = MagicMock()
        config.logging_enabled.return_value = logging_enabled
        config.serverless.return_value = serverless
        config.logger_min_level.return_value = logging.INFO
        config.verbose_mode.return_value = False
        config.debug_mode.return_value = False
        config.file_logging_enabled.return_value = False
        config.console_handler_enabled.return_value = False
        return config

    def test_logging_disabled_returns_null_handler(self):
        config = self._make_mock_config(logging_enabled=False)
        logger = logging_init(config, None, None)
        # Should have NullHandler
        assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)
        assert logger.level == logging.CRITICAL

    def test_serverless_returns_lambda_logger(self):
        config = self._make_mock_config(serverless=True)
        logger = logging_init(config, None, None)
        assert logger.name == BASE_LOGGER_NAME

    def test_logging_enabled_initializes(self):
        config = self._make_mock_config(logging_enabled=True)
        config.console_handler_enabled.return_value = False
        # Reset to ensure fresh init
        base = logging.getLogger(BASE_LOGGER_NAME)
        base.handlers.clear()
        base._baselogger_initialized = False

        logger = logging_init(config, None, None)
        assert logger.name == BASE_LOGGER_NAME
        assert not logger.propagate
