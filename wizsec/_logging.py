##
##
##   L        OOOOO    GGGGG    GGGGG   IIIIIII  N     N    GGGGG
##   L       O     O  G        G           I     NN    N   G
##   L       O     O  G   GGG  G   GGG     I     N N   N   G   GGG
##   L       O     O  G     G  G     G     I     N  N  N   G     G
##   LLLLLL   OOOOO    GGGGG    GGGGG   IIIIIII  N     N    GGGGG
##
##
##

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Union

VERBOSE_LEVEL = 15  # Between INFO (20) and DEBUG (10)
BASE_LOGGER_NAME = "wizsec"


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
    """Formatter that uses datetime for microsecond-precision timestamps."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Format the record's creation time with microsecond precision."""
        ct = datetime.fromtimestamp(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        else:
            return ct.strftime('%Y-%m-%d %H:%M:%S.%f')


class MarkdownFormatter(CustomFormatter):
    """Formatter that outputs log records as Markdown table rows."""

    header_written: bool = False

    def format(self, record: logging.LogRecord) -> str:
        """Format the record as a Markdown table row, prepending the header on first call."""
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


def _ensure_verbose_console_level(level: int, config: Any) -> int:
    """Lower the console level to VERBOSE if verbose mode is enabled."""
    if config.verbose_mode() and level > logging.VERBOSE:
        return logging.VERBOSE
    return level


def _init_lambda_logger(level: int) -> logging.Logger:
    """Initialize a minimal stdout logger for serverless/Lambda environments."""
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


def _maybe_attach_console_handler(logger: logging.Logger, config: Any) -> None:
    """Attach a console (stdout) handler to the logger if enabled in config."""
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


def _maybe_attach_file_handler(
    logger: logging.Logger,
    config: Any,
    default_wiz_dir: Path,
    parse_filepath_fn: Callable[..., Any],
) -> None:
    """Attach a file handler to the logger if file logging is enabled."""
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


def logging_init(
    config: Any,
    default_wiz_dir: Path,
    parse_filepath_fn: Callable[..., Any],
    name: str = BASE_LOGGER_NAME,
) -> logging.Logger:
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
