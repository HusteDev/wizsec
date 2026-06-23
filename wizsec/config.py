##
##
##   CCCCC    OOOOO   N     N   FFFFFF   IIIIIII   GGGGG
##  C        O     O  NN    N   F           I     G
##  C        O     O  N N   N   FFFFF       I     G   GGG
##  C        O     O  N  N  N   F           I     G     G
##   CCCCC    OOOOO   N     N   F        IIIIIII   GGGGG
##
##
##

# config.py
from pathlib import Path
import re
import yaml
import sys
import os
import logging
import tempfile
import inspect
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .version import __version__ as wizsec_version
from ._logging import (
    logging_init as _logging_init,
    parse_level as _parse_level,
    BASE_LOGGER_NAME,
)

LIBRARY_NAME = "wizsec"
CURRENT_VERSION = wizsec_version

# Increment this when the config file structure changes (fields added, renamed, removed).
# Adding a migration function to _MIGRATIONS is required for each bump.
CONFIG_SCHEMA_VERSION = 2

# Keys are (from_schema, to_schema). Each function receives the in-memory config
# dict, modifies it in place, and returns a list of human-readable change descriptions.


def _migrate_v1_to_v2(config: Dict[str, Any]) -> List[str]:
    """Rename top-level 'saved_data' config section to 'cache'."""
    if "saved_data" in config:
        config["cache"] = config.pop("saved_data")
        return ["Renamed config section 'saved_data' to 'cache'"]
    return []


_MIGRATIONS: Dict[Tuple[int, int], Callable[[Dict[str, Any]], List[str]]] = {
    (1, 2): _migrate_v1_to_v2,
}

# _CONFIG = None
SERVERLESS = str(os.environ.get("WIZ_SERVERLESS", "")).lower() in (
    "1",
    "true",
    "yes",
) or bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DEFAULT_WIZ_DIR = Path("/var/task/.wiz") if SERVERLESS else Path.home() / ".wiz"
DEFAULT_TEMP_FOLDER = "/tmp" if SERVERLESS else Path(tempfile.gettempdir())
CWD = "/tmp" if SERVERLESS else str(os.getcwd()).replace("\\", "/")


def _update_config_schema_in_file(config_file_path: Path, schema_version: int) -> bool:
    """Insert or update config_schema in the config file, preserving comments and formatting."""
    try:
        content = config_file_path.read_text(encoding="utf-8")
        if re.search(r"^\s+config_schema:", content, re.MULTILINE):
            content = re.sub(
                r"([ \t]+config_schema:[ \t]*)\d+",
                lambda m: m.group(1) + str(schema_version),
                content,
            )
        else:
            content = re.sub(
                r"([ \t]+release:[ \t]*[^\n]*\n)",
                r"\1  config_schema: " + str(schema_version) + "\n",
                content,
                count=1,
            )
        config_file_path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def _run_migrations(config: Dict[str, Any], config_file_path: Path) -> None:
    """Apply any pending config schema migrations and persist the updated file."""
    file_schema = int(config.get("app", {}).get("config_schema", 0))
    if file_schema >= CONFIG_SCHEMA_VERSION:
        return

    all_changes: List[str] = []
    current = file_schema
    while current < CONFIG_SCHEMA_VERSION:
        next_ver = current + 1
        migration_fn = _MIGRATIONS.get((current, next_ver))
        if migration_fn:
            all_changes.extend(migration_fn(config))
        current = next_ver

    config.setdefault("app", {})["config_schema"] = CONFIG_SCHEMA_VERSION
    _update_config_schema_in_file(config_file_path, CONFIG_SCHEMA_VERSION)

    if all_changes:
        msg = f"Info: Config migrated to schema v{CONFIG_SCHEMA_VERSION}.\n"
        for change in all_changes:
            msg += f"  - {change}\n"
        sys.stdout.write(msg)
        sys.stdout.flush()


class Config:
    _CONFIG = None
    _logger = None
    _loaded = False

    @classmethod
    def load(
        cls,
        config_path: Optional[str] = None,
        overrides: Optional[Dict[str, str]] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        """Load configuration from YAML file, applying overrides and credential env vars.

        Args:
            config_path: Path to the YAML config file. Defaults to ~/.wiz/wiz.config.
            overrides: Dot-notation key=value strings to override config values.
            client_id: If provided, sets WIZ_CLIENT_ID environment variable.
            client_secret: If provided, sets WIZ_CLIENT_SECRET environment variable.
        """
        if cls._CONFIG is None:
            config_path = config_path or str(DEFAULT_WIZ_DIR / "wiz.config")
            path, filename = parse_filepath(str(config_path))
            if all(
                [
                    path,
                    path.is_dir(),
                    filename,
                    isinstance(filename, str),
                    os.path.exists(path / filename),  # type: ignore[operator]
                ]
            ):
                with open(path / filename, "r") as file:  # type: ignore[operator]
                    cls._CONFIG = yaml.safe_load(file)
            else:
                if not SERVERLESS:
                    created = generate_default_config(str(config_path))
                    if created:
                        with open(str(config_path), "r") as file:
                            cls._CONFIG = yaml.safe_load(file)
                else:
                    raise Exception(
                        "Create config file at /var/task/.wiz for all Serverless executions"
                    )

            # Apply overrides via dot-notation keys
            for item in overrides or {}:
                if isinstance(item, str) and "=" in item:
                    key, value = item.split("=", 1)
                    value = yaml.safe_load(value)
                elif isinstance(item, str):
                    continue
                else:
                    continue
                keys = key.split(".")
                d: Any = cls._CONFIG
                for k in keys[:-1]:
                    if k not in d or not isinstance(d[k], dict):
                        d[k] = {}
                    d = d[k]
                d[keys[-1]] = value

            if client_id:
                os.environ["WIZ_CLIENT_ID"] = client_id
            if client_secret:
                os.environ["WIZ_CLIENT_SECRET"] = client_secret

            if not cls.serverless():
                assert cls._CONFIG is not None
                _config_file = path / filename if filename else None
                if _config_file and _config_file.exists():
                    _run_migrations(cls._CONFIG, _config_file)

                # CA Certificate
                ca_file_path, filename = parse_filepath(
                    cls._CONFIG.get("auth", {}).get("ca_cert", "")
                )
                if ca_file_path and filename:
                    cert_path = str(Path(ca_file_path) / filename)
                    os.environ["REQUESTS_CA_BUNDLE"] = cert_path
                else:
                    ca_env_vars = [
                        "REQUESTS_CA_BUNDLE",
                        "CURL_CA_BUNDLE",
                        "SSL_CERT_FILE",
                        "PIP_CERT",
                        "AWS_CA_BUNDLE",
                        "GIT_SSL_CAINFO",
                        "NODE_EXTRA_CA_CERTS",
                    ]
                    existing_cert = None
                    for var in ca_env_vars:
                        val = os.environ.get(var)
                        if val:
                            existing_cert = val
                            break
                    if existing_cert:
                        os.environ["REQUESTS_CA_BUNDLE"] = existing_cert
        cls._loaded = True
        if cls._logger is None:
            cls._logger = _logging_init(
                cls, DEFAULT_WIZ_DIR, parse_filepath, name="wizsec"
            )

    def ensure_loaded(func: Callable) -> Callable:  # type: ignore[misc]
        """Decorator that ensures Config.load() has been called before method execution."""

        @wraps(func)
        def wrapper(cls: "Config", *args: Any, **kwargs: Any) -> Any:
            if not cls._loaded:
                cls.load()
            return func(cls, *args, **kwargs)

        return wrapper

    @classmethod
    @ensure_loaded
    def get_logger(cls) -> logging.Logger:
        """Return a child logger named after the calling module."""
        base_logger = cls._logger or _logging_init(cls, DEFAULT_WIZ_DIR, parse_filepath)

        # Determine caller’s module to create a child-like name
        stack = inspect.stack()
        caller_frame = stack[2]  # 0=this, 1=wrapper, 2=caller
        caller_file = caller_frame.filename
        caller_file_name = os.path.splitext(os.path.basename(caller_file))[0]
        child_name = f"{BASE_LOGGER_NAME}.{caller_file_name}"

        logger = logging.getLogger(child_name)

        # Idempotent attach: copy handler refs and level from base (but not the name)
        if not getattr(logger, "_baselogger_initialized", False):
            logger.handlers = list(base_logger.handlers)  # same handler objects
            logger.setLevel(base_logger.level)
            logger.propagate = False  # don’t bubble to root
            logger._baselogger_initialized = True  # type: ignore[attr-defined]
        else:
            # keep level in sync with base, in case config changed on a warm start
            logger.setLevel(base_logger.level)

        return logger

    @classmethod
    @ensure_loaded
    def load_dotenv(cls) -> None:
        """Load environment variables from the .env file in the configured env_path."""
        if cls.serverless():
            return  # skip loading files in serverless mode
        from dotenv import load_dotenv

        env_path, _ = parse_filepath(cls.env_path())
        dotenv_file = str(env_path / ".env")
        if os.path.exists(dotenv_file):
            load_dotenv(dotenv_path=dotenv_file)

    @classmethod
    @ensure_loaded
    def get(cls, *keys: str, default: Any = None) -> Any:
        """Retrieve a nested config value by key path.

        Args:
            *keys: Sequence of keys to traverse in the config dict.
            default: Value returned when the key path is not found.
        """
        d = cls._CONFIG
        for key in keys:
            d = d.get(key, {}) if isinstance(d, dict) else {}
        result = d if d else default
        if cls._logger:
            cls.get_logger().debug(f"get() called with keys={keys} -> {result}")
        return result

    @classmethod
    @ensure_loaded
    def set(cls, *keys: str, value: Any) -> None:
        """Set a nested config value by key path, creating intermediate dicts as needed.

        Runtime override only — does not persist back to the YAML config file.

        Args:
            *keys: Sequence of keys to traverse; the last key is the leaf to assign.
            value: Value to store at the leaf.

        Raises:
            ValueError: If no keys are provided.
        """
        if not keys:
            raise ValueError("Config.set() requires at least one key")
        if cls._CONFIG is None:
            cls._CONFIG = {}
        d = cls._CONFIG
        for key in keys[:-1]:
            existing = d.get(key)
            if not isinstance(existing, dict):
                existing = {}
                d[key] = existing
            d = existing
        d[keys[-1]] = value
        if cls._logger:
            cls.get_logger().debug(f"set() called with keys={keys} -> {value}")

    @classmethod
    @ensure_loaded
    def set_log_level(
        cls,
        level: Union[int, str],
        *,
        handler_level: Optional[Union[int, str]] = None,
        include_children: bool = True,
    ) -> int:
        """
        Change the library logging level at runtime.

        - `level`: logger level for base and children (e.g. 'DEBUG', 20, 'VERBOSE')
        - `handler_level`: optional handler threshold; defaults to `level`
        - `include_children`: also retune wizsec.<module> child loggers

        Returns the effective base level as an int.
        """
        # Make sure base logger exists
        base_logger = cls._logger or _logging_init(cls, DEFAULT_WIZ_DIR, parse_filepath)
        base_logger_name = getattr(base_logger, "name", BASE_LOGGER_NAME)

        new_level = _parse_level(level, default=logging.INFO)  # from _logging module
        new_hlevel = _parse_level(handler_level, default=new_level)

        # 1) Update base logger + its handlers
        base_logger.setLevel(new_level)
        for h in list(base_logger.handlers):
            h.setLevel(new_hlevel)

        # 2) Optionally update already-created children
        if include_children:
            mgr = logging.Logger.manager
            prefix = base_logger_name + "."
            for name, lg in list(mgr.loggerDict.items()):
                if not isinstance(lg, logging.Logger):
                    continue
                if name == base_logger_name or name.startswith(prefix):
                    lg.setLevel(new_level)
                    # Children created by get_logger() share handler objects with base.
                    # But if a child somehow has its own handlers, align those too.
                    for h in list(lg.handlers):
                        h.setLevel(new_hlevel)

        # Keep Config._logger reference aligned
        cls._logger = base_logger
        return new_level

    ##########################
    # Config File Properties #
    ##########################

    ############
    # App
    ############
    @classmethod
    @ensure_loaded
    def app_name(cls) -> str:
        """Return the configured application name."""
        return cls.get("app", "name", default="wizsec")

    @classmethod
    @ensure_loaded
    def release_version(cls) -> str:
        """Return the configured release version string."""
        return cls.get("app", "release", default="0.0.0")

    @classmethod
    def serverless(cls) -> bool:
        """Return whether the SDK is running in serverless mode."""
        return SERVERLESS

    @classmethod
    @ensure_loaded
    def wiz_dir(cls) -> Path:
        """Return the resolved Wiz configuration directory path."""
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(cls.get("app", "wiz_dir", default=DEFAULT_WIZ_DIR))
        cls.get_logger().debug(f"Resolved wiz_dir: {filepath}")
        return filepath

    @classmethod
    @ensure_loaded
    def env_path(cls) -> Path:
        """Return the resolved path to the .env file directory."""
        filepath, _ = parse_filepath(cls.get("app", "env_path", default=f"{CWD}"))
        cls.get_logger().debug(f"Resolved env_path: {filepath}")
        return filepath

    ############
    # Cache
    ############
    @classmethod
    @ensure_loaded
    def cache_directory(cls) -> Path:
        """Return the resolved directory path for filesystem cache output."""
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(
            cls.get("cache", "directory", default=str(DEFAULT_WIZ_DIR / ".cache"))
        )
        return filepath

    @classmethod
    @ensure_loaded
    def cache_pickle_enabled(cls) -> bool:
        """Return whether pickle serialization is enabled for filesystem cache."""
        if cls.serverless():
            return False
        return cls.get("cache", "pickle", default=False)

    ############
    # Auth
    ############
    @classmethod
    @ensure_loaded
    def grant_type(cls) -> str:
        """Return the configured OAuth grant type."""
        if cls.serverless():
            return "client_credentials"
        return cls.get("auth", "grant_type", default="client_credentials")

    @classmethod
    @ensure_loaded
    def storage_method(cls) -> str:
        """Return the credential storage method (file, env, or prompt)."""
        return cls.get("auth", "credentials", "storage_method", default="file")

    @classmethod
    @ensure_loaded
    def credential_file_path(cls) -> Path:
        """Return the resolved path to the credentials file directory."""
        filepath, _ = parse_filepath(
            cls.get("auth", "credentials", "file_path", default=f"{DEFAULT_WIZ_DIR}")
        )
        cls.get_logger().debug(f"Resolved credential file path: {filepath}")
        return filepath

    @classmethod
    @ensure_loaded
    def ca_cert(cls) -> str:
        """Return the configured CA certificate path."""
        return cls.get("auth", "ca_cert", default="")

    @classmethod
    @ensure_loaded
    def quiet_auth(cls) -> str:
        """Return whether device-code auth should suppress browser prompts."""
        return cls.get("auth", "device", "quiet", default="true")

    @classmethod
    @ensure_loaded
    def auth_poll_time(cls) -> int:
        """Return the polling interval in seconds for device-code auth."""
        return int(cls.get("auth", "device", "poll_time", default=5))

    @classmethod
    @ensure_loaded
    def get_proxies(cls) -> Dict[str, Optional[str]]:
        """Return a dict of HTTP/HTTPS proxy URLs from config or environment."""
        if cls.serverless():
            return {"https": os.environ.get("HTTPS_PROXY")}
        proxy_config = cls.get("auth", "proxy", default={})
        return {
            "http": (
                f'{proxy_config.get("http", {}).get("url", "")}:{proxy_config.get("http", {}).get("port", "")}'
                if proxy_config
                else os.environ.get("HTTP_PROXY")
            ),
            "https": (
                f'{proxy_config.get("https", {}).get("url", "")}:{proxy_config.get("https", {}).get("port", "")}'
                if proxy_config
                else os.environ.get("HTTPS_PROXY")
            ),
        }

    ############
    # Domain
    ############
    @classmethod
    @ensure_loaded
    def default_domain(cls) -> str:
        """Return the default Wiz domain identifier."""
        return cls.get("domain", "default", default="gov")

    @classmethod
    @ensure_loaded
    def domain_enabled(cls, domain: str) -> bool:
        """Return whether the specified domain is enabled in config."""
        return cls.get("domain", domain, "enabled", default=False)

    @classmethod
    @ensure_loaded
    def domain_root(cls, env: str) -> Optional[str]:
        """Return the root domain string for the given environment identifier."""
        root_domains = {
            "app": "app.wiz.io",
            "gov": "gov.wiz.io",
            "fedramp": "app.wiz.us",
        }
        return root_domains.get(env, root_domains.get(cls.default_domain()))

    ############
    # API
    ############
    @classmethod
    @ensure_loaded
    def api_max_retries(cls) -> int:
        """Return the maximum number of API request retries."""
        return cls.get("api", "max_retries", default=5)

    @classmethod
    @ensure_loaded
    def api_retry(cls) -> int:
        """Return the retry delay in seconds between API attempts."""
        return cls.get("api", "retry_time", default=2)

    @classmethod
    @ensure_loaded
    def api_timeout(cls) -> int:
        """Return the API request timeout in seconds."""
        return cls.get("api", "timeout", default=180)

    @classmethod
    @ensure_loaded
    def validate_queries(cls) -> bool:
        """Return whether GraphQL query validation is enabled."""
        return cls.get("api", "validate_queries", default=False)

    ############
    # Reports
    ############
    @classmethod
    @ensure_loaded
    def report_stream_by_default(cls) -> bool:
        """Return whether reports should stream results by default."""
        return cls.get("reports", "stream_by_default", default=True)

    @classmethod
    @ensure_loaded
    def report_export_directory(cls) -> Path:
        """Return the resolved directory path for report exports."""
        filepath, _ = parse_filepath(
            cls.get("reports", "export_directory", default=f"{CWD}")
        )
        cls.get_logger().debug(f"Resolved report export directory: {filepath}")
        return filepath

    @classmethod
    @ensure_loaded
    def report_export_type(cls) -> str:
        """Return the default export format for reports."""
        return cls.get("reports", "export_type", default="json")

    @classmethod
    @ensure_loaded
    def report_retry_time(cls) -> int:
        """Return the retry delay in seconds for report operations."""
        return cls.get("reports", "retry_time", default=30)

    @classmethod
    @ensure_loaded
    def report_max_retries(cls) -> int:
        """Return the maximum number of retries for report operations."""
        return cls.get("reports", "max_retries", default=3)

    @classmethod
    @ensure_loaded
    def report_polling_time(cls) -> int:
        """Return the polling interval in seconds for report status checks."""
        return cls.get("reports", "polling_time", default=15)

    @classmethod
    @ensure_loaded
    def report_auto_cleanup(cls) -> bool:
        """Return whether automatic report cleanup is enabled."""
        return cls.get("reports", "auto_cleanup", default=False)

    @classmethod
    @ensure_loaded
    def report_save_incomplete(cls) -> bool:
        """Return whether incomplete reports should be saved to disk."""
        return cls.get("reports", "save_incomplete_reports", default=True)

    ############
    # Logging
    ############
    @classmethod
    @ensure_loaded
    def logging_enabled(cls) -> bool:
        """Return whether logging is enabled."""
        val = cls.get("logging", "enabled", default=True)
        return val

    @classmethod
    @ensure_loaded
    def logger_min_level(cls) -> int:
        """Return the minimum logging level threshold."""
        val = cls.get("logging", "lowest_level", default=10)
        return val

    @classmethod
    @ensure_loaded
    def verbose_mode(cls) -> bool:
        """Return whether verbose logging mode is enabled."""
        val = cls.get("logging", "verbose", default=False)
        return val

    @classmethod
    @ensure_loaded
    def debug_mode(cls) -> bool:
        """Return whether debug logging mode is enabled."""
        val = cls.get("logging", "debug", default=False)
        return val

    @classmethod
    @ensure_loaded
    def file_logging_enabled(cls) -> bool:
        """Return whether file-based log output is enabled."""
        if cls.serverless():
            return False
        val = cls.get("logging", "file_handler", "enabled", default=False)
        return val

    @classmethod
    @ensure_loaded
    def file_handler_logging_level(cls) -> str:
        """Return the logging level for the file handler."""
        return cls.get("logging", "file_handler", "logging_level", default="DEBUG")

    @classmethod
    @ensure_loaded
    def file_handler_log_directory(cls) -> Union[str, Path]:
        """Return the resolved directory path for log file output."""
        if cls.serverless():
            return "/tmp"
        path_str = cls.get("logging", "file_handler", "log_directory", default="")
        if not path_str:  # if empty string from config
            return DEFAULT_WIZ_DIR
        filepath, _ = parse_filepath(path_str)
        return filepath

    @classmethod
    @ensure_loaded
    def file_handler_create_log_dir(cls) -> bool:
        """Return whether the log directory should be auto-created."""
        return cls.get("logging", "file_handler", "create_log_dir", default=True)

    @classmethod
    @ensure_loaded
    def file_handler_markdown_enabled(cls) -> bool:
        """Return whether markdown-formatted log output is enabled."""
        return cls.get("logging", "file_handler", "markdown", default=False)

    @classmethod
    @ensure_loaded
    def console_handler_enabled(cls) -> bool:
        """Return whether console log output is enabled."""
        return cls.get(
            "logging", "console_handler", "enabled", default=cls.logging_enabled()
        )

    @classmethod
    @ensure_loaded
    def console_handler_logging_level(cls) -> str:
        """Return the logging level for the console handler."""
        return cls.get("logging", "console_handler", "logging_level", default="INFO")

    ##################
    # Query Splitting
    ##################
    @classmethod
    @ensure_loaded
    def query_splitting_enabled(cls) -> bool:
        """Return whether pre-emptive totalCount probing and query splitting is enabled."""
        return cls.get("query_splitting", "enabled", default=False)

    @classmethod
    @ensure_loaded
    def query_splitting_threshold(cls) -> int:
        """Return the record count above which splitting is triggered."""
        return int(cls.get("query_splitting", "threshold", default=10000))

    @classmethod
    @ensure_loaded
    def query_splitting_max_concurrent(cls) -> int:
        """Return the maximum number of concurrent async sub-queries when splitting."""
        return int(cls.get("query_splitting", "max_concurrent", default=10))

    @classmethod
    @ensure_loaded
    def query_splitting_detection_mode(cls) -> str:
        """Return the totalCount detection mode: 'static' or 'schema'."""
        return str(cls.get("query_splitting", "detection_mode", default="static"))

    @classmethod
    @ensure_loaded
    def query_splitting_split_by(cls) -> str:
        """Return the dimension to split by: 'cloudAccounts' or 'projects'."""
        return str(cls.get("query_splitting", "split_by", default="cloudAccounts"))

    @classmethod
    @ensure_loaded
    def query_splitting_filter_path(cls) -> str:
        """Return the dot-notation path in vars where subscription IDs are injected."""
        return str(cls.get("query_splitting", "filter_path", default=""))

    @classmethod
    @ensure_loaded
    def query_splitting_cache_subscriptions(cls) -> bool:
        """Return whether the subscription/project list should be cached per session."""
        return cls.get("query_splitting", "cache_subscriptions", default=True)


def generate_default_config(
    file_path: Union[str, Path] = (DEFAULT_WIZ_DIR / "wiz.config"),
) -> bool:
    """Generate a default config file from the bundled template.

    Args:
        file_path: Destination path for the generated config file.

    Returns:
        True if the config file was created, False in serverless mode.
    """
    if SERVERLESS:
        return False

    template_path = Path(__file__).parent / "wiz.config.template"
    yaml_content = template_path.read_text(encoding="utf-8")
    yaml_content = yaml_content.replace("${SDK_NAME}", LIBRARY_NAME)
    yaml_content = yaml_content.replace("${SDK_VERSION}", CURRENT_VERSION)
    yaml_content = yaml_content.replace(
        "${CONFIG_SCHEMA_VERSION}", str(CONFIG_SCHEMA_VERSION)
    )

    if not DEFAULT_WIZ_DIR.exists():
        DEFAULT_WIZ_DIR.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w") as file:
        file.write(yaml_content)

    if not os.path.exists(DEFAULT_WIZ_DIR / "wiz.credentials"):
        Path(str(DEFAULT_WIZ_DIR / "wiz.credentials")).touch()

    sys.stdout.write(f"New config file created at {file_path}\n")
    sys.stdout.flush()

    return True


def expand_all_vars(path_str: str) -> str:
    """Expand $CWD, $HOME, ~, and environment variables in a path string."""
    path_str = str(path_str)  # ensure it's a string
    path_str = path_str.replace("$CWD", str(Path.cwd()))
    path_str = path_str.replace("$HOME", str(Path.home()))
    expanded = os.path.expanduser(path_str)
    expanded = os.path.expandvars(expanded)
    return expanded


def parse_filepath(path_str: Optional[str] = None) -> Tuple[Path, Optional[str]]:
    """Parse and resolve a path string into a directory and optional filename.

    Args:
        path_str: Raw path string, possibly containing variables. Defaults to cwd.

    Returns:
        Tuple of (directory Path, filename string or None).
    """
    if path_str is None:
        return Path.cwd(), None
    expanded_path_str = expand_all_vars(path_str)
    path = Path(expanded_path_str)
    if not path.is_absolute():
        path = Path.cwd() / path

    if path.suffix:
        # In serverless mode, don't try to create directories
        if not Config.serverless() and not path.parent.is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
        return path.parent, path.name
    else:
        # In serverless mode, don't try to create directories
        if not Config.serverless() and not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
        return path, None
