"""
Logging setup for wiz-sdk.

Provides the VERBOSE log level, custom formatters, and the logging_init()
function that Config.get_logger() delegates to.
"""

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Union

VERBOSE_LEVEL = 15  # Between INFO (20) and DEBUG (10)
BASE_LOGGER_NAME = "wiz_sdk"


def _register_verbose_level() -> None:
    """Register the custom VERBOSE level once."""
    if not hasattr(logging, "VERBOSE"):
        logging.addLevelName(VERBOSE_LEVEL, "VERBOSE")

        def _verbose(self, message, *args, **kwargs):
            if self.isEnabledFor(VERBOSE_LEVEL):
                self._log(VERBOSE_LEVEL, message, args, **kwargs)

        logging.Logger.verbose = _verbose
        logging.VERBOSE = VERBOSE_LEVEL


class CustomFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        else:
            return ct.strftime('%Y-%m-%d %H:%M:%S.%f')


class MarkdownFormatter(CustomFormatter):
    header_written = False

    def format(self, record):
        formatted_message = super().format(record)
        markdown_message = formatted_message

        if not MarkdownFormatter.header_written:
            header = (
                '| Timestamp | Level | File | Function | Line | Thread ID | Thread Name | Message |\n'
                '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
            )
            MarkdownFormatter.header_written = True
            markdown_message = header + markdown_message

        return markdown_message


def _ensure_verbose_console_level(level: int, config) -> int:
    if config.verbose_mode() and level > logging.VERBOSE:
        return logging.VERBOSE
    return level


def _init_lambda_logger(level: int) -> logging.Logger:
    logger = logging.getLogger(BASE_LOGGER_NAME)
    if getattr(logger, "_baselogger_initialized", False):
        logger.setLevel(level)
        return logger

    logger.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(level)
    h.setFormatter(logging.Formatter('[%(levelname)s] %(name)s: %(message)s'))
    logger.addHandler(h)
    logger.setLevel(level)
    logger.propagate = False
    logger._baselogger_initialized = True
    return logger


def _maybe_attach_console_handler(logger: logging.Logger, config) -> None:
    if not config.console_handler_enabled():
        return
    lvl_name = str(config.console_handler_logging_level()).upper()
    lvl = getattr(logging, lvl_name, config.logger_min_level())
    lvl = _ensure_verbose_console_level(lvl, config)

    h = logging.StreamHandler(stream=sys.stdout)
    h.setLevel(lvl)
    fmt = '\r%(asctime)s [%(name)s::%(levelname)s]:   %(message)s\033[K'
    h.setFormatter(logging.Formatter(fmt=fmt, datefmt='%H:%M:%S'))
    logger.addHandler(h)
    logger.console_handler = h


def _maybe_attach_file_handler(logger: logging.Logger, config, default_wiz_dir, parse_filepath_fn) -> None:
    if not (config.file_logging_enabled() and not config.serverless()):
        return

    cfg_dir = config.get("logging", "file_handler", "log_directory", default="")
    base_dir = default_wiz_dir if not cfg_dir else parse_filepath_fn(cfg_dir)[0]
    log_dir = base_dir / "logs"

    if config.file_handler_create_log_dir() and not log_dir.is_dir():
        log_dir.mkdir(parents=True, exist_ok=True)

    fname = f'{date.today():%Y%m%d}_{datetime.now():%H%M%S}_{Path(sys.argv[0]).stem}.log'
    path = log_dir / fname

    logger.log_directory = log_dir

    h = logging.FileHandler(filename=path, mode="w")
    lvl_name = str(config.file_handler_logging_level()).upper()
    lvl = getattr(logging, lvl_name, logging.DEBUG)
    h.setLevel(lvl)

    datefmt = '%d-%b-%y %H:%M:%S.%f'
    if config.file_handler_markdown_enabled():
        fmt = '| %(asctime)s | %(levelname)s | %(filename)s | %(funcName)s | %(lineno)d | %(thread)d | %(threadName)s | %(message)s |'
        h.setFormatter(MarkdownFormatter(fmt=fmt, datefmt=datefmt))
    else:
        fmt = '%(asctime)s  [%(levelname)s] - (%(name)s):  %(message)s'
        h.setFormatter(CustomFormatter(fmt=fmt, datefmt=datefmt))

    logger.addHandler(h)
    logger.file_handler = h


def logging_init(config, default_wiz_dir, parse_filepath_fn, name: str = BASE_LOGGER_NAME) -> logging.Logger:
    """Initialize or return the library logger.

    Args:
        config: The Config class (passed to avoid circular imports).
        default_wiz_dir: DEFAULT_WIZ_DIR path.
        parse_filepath_fn: The parse_filepath function.
        name: Logger name hint.
    """
    _register_verbose_level()

    try:
        cfg_level = config.logger_min_level()
        base_level = int(cfg_level) if isinstance(cfg_level, int) else getattr(logging, str(cfg_level).upper(), logging.INFO)
    except Exception:
        base_level = logging.INFO

    if config.serverless():
        return _init_lambda_logger(base_level)

    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger

    logger = logging.getLogger(BASE_LOGGER_NAME)

    if not config.logging_enabled():
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
        return logger

    if not getattr(logger, "_baselogger_initialized", False):
        logger.handlers.clear()
        logger.setLevel(base_level)
        logger.propagate = False
        _maybe_attach_console_handler(logger, config)
        _maybe_attach_file_handler(logger, config, default_wiz_dir, parse_filepath_fn)
        logger._baselogger_initialized = True
    else:
        logger.setLevel(base_level)

    logger.verbose(
        "Logging enabled: %s | Root Level: %s | Debug Mode: %s | Verbose Logging: %s | File Logging: %s",
        config.logging_enabled(), config.logger_min_level(), config.debug_mode(),
        config.verbose_mode(), config.file_logging_enabled()
    )
    return logger


def parse_level(level: Union[int, str, None], default=logging.INFO) -> int:
    """Parse a log level from int, string name, or None."""
    if level is None:
        return default
    if isinstance(level, int):
        return level
    try:
        if str(level).isdigit():
            return int(level)
        name = str(level).upper()
        if name == "VERBOSE":
            return getattr(logging, "VERBOSE", 15)
        return getattr(logging, name, default)
    except Exception:
        return default
