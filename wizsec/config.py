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
from .exceptions import WizConfigurationError
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


def _migrate_v1_to_v2(config: Dict[str, Any]) -> List[str]:
    """Populate v2 config defaults without overriding user-provided values."""
    changes: List[str] = []

    app = config.setdefault("app", {})
    if "config_schema" not in app:
        changes.append("Added app.config_schema")

    api = config.setdefault("api", {})
    if "auto_paginate" not in api:
        api["auto_paginate"] = True
        changes.append("Added api.auto_paginate (default: true)")

    domain = config.setdefault("domain", {})
    if "default" not in domain:
        domain["default"] = "gov"
        changes.append("Added domain.default (default: gov)")

    for name, enabled in (("app", False), ("gov", True), ("fedramp", False)):
        section = domain.setdefault(name, {})
        if "enabled" not in section:
            section["enabled"] = enabled
            changes.append(f"Added domain.{name}.enabled (default: {enabled})")

    auth = config.setdefault("auth", {})
    proxy = auth.setdefault("proxy", {})
    for scheme in ("http", "https"):
        section = proxy.setdefault(scheme, {})
        if "url" not in section:
            section["url"] = ""
            changes.append(f"Added auth.proxy.{scheme}.url")
        if "port" not in section:
            section["port"] = 80
            changes.append(f"Added auth.proxy.{scheme}.port")

    return changes


# Keys are (from_schema, to_schema). Each function receives the in-memory config
# dict, modifies it in place, and returns a list of human-readable change descriptions.
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


def _ensure_config_line_after(
    content: str, anchor_pattern: str, line_template: str, presence_pattern: str
) -> str:
    """Insert a single config line after an anchor when it is not already present."""
    if re.search(presence_pattern, content, re.MULTILINE):
        return content
    return re.sub(
        anchor_pattern,
        lambda m: m.group(0) + line_template.format(indent=m.group(1)),
        content,
        count=1,
    )


def _update_config_v2_in_file(config_file_path: Path) -> bool:
    """Apply text-preserving v2 config-file updates for existing user configs."""
    try:
        content = config_file_path.read_text(encoding="utf-8")
        content = _ensure_config_line_after(
            content,
            r"(?m)^([# ]*)  timeout:[^\n]*\n",
            "{indent}  auto_paginate: true               "
            "# Automatically paginate through results | default = true\n",
            r"^\s*#?\s*auto_paginate:",
        )
        content = _ensure_config_line_after(
            content,
            r"(?m)^([# ]*)  gov:\s*\n(?:\1    enabled:[^\n]*\n)?",
            "{indent}  fedramp:\n{indent}    enabled: false\n",
            r"^\s*#?\s*fedramp:",
        )
        content = content.replace(
            "Custom Proxy Settings if necessary | default = use system settings",
            "Custom Proxy Settings if necessary; blank URLs use environment proxies",
        )
        config_file_path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def _run_migrations(
    config: Dict[str, Any], config_file_path: Optional[Path] = None
) -> None:
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
    if config_file_path is not None:
        _update_config_schema_in_file(config_file_path, CONFIG_SCHEMA_VERSION)
        if file_schema < 2 <= CONFIG_SCHEMA_VERSION:
            _update_config_v2_in_file(config_file_path)

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

            assert cls._CONFIG is not None
            _config_file = path / filename if filename else None
            _run_migrations(
                cls._CONFIG,
                _config_file if _config_file and _config_file.exists() else None,
            )

            if not cls.serverless():
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
        if not keys:
            return cls._CONFIG if cls._CONFIG is not None else default

        d = cls._CONFIG
        for key in keys:
            if not isinstance(d, dict) or key not in d:
                result = default
                break
            d = d[key]
        else:
            result = d
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
    # Save
    ############
    @classmethod
    @ensure_loaded
    def saved_data_enabled(cls) -> bool:
        """Return whether saved data persistence is enabled."""
        if cls.serverless():
            return False
        return cls.get("saved_data", "enabled", default=True)

    @classmethod
    @ensure_loaded
    def saved_data_directory(cls) -> Path:
        """Return the resolved directory path for saved data output."""
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(
            cls.get("saved_data", "directory", default=f"{CWD}/saved-data")
        )
        return filepath

    @classmethod
    @ensure_loaded
    def saved_data_temp_directory(cls) -> Path:
        """Return the resolved temporary directory path for saved data."""
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(
            cls.get("saved_data", "temp", default=DEFAULT_TEMP_FOLDER)
        )
        return filepath

    @classmethod
    @ensure_loaded
    def saved_data_pickle_enabled(cls) -> bool:
        """Return whether pickle serialization is enabled for saved data."""
        if cls.serverless():
            return False
        return cls.get("saved_data", "pickle", default=False)

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

        def _proxy_url(scheme: str, env_var: str) -> Optional[str]:
            section = proxy_config.get(scheme, {}) if proxy_config else {}
            url = str(section.get("url", "") or "").strip()
            port = section.get("port")
            if not url:
                return os.environ.get(env_var)
            return f"{url}:{port}" if port else url

        return {
            "http": _proxy_url("http", "HTTP_PROXY"),
            "https": _proxy_url("https", "HTTPS_PROXY"),
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
    def validate_domain(cls, env: str) -> str:
        """Validate and return the root domain for a configured Wiz environment."""
        root = cls.domain_root(env)
        if root is None:
            raise WizConfigurationError(
                f"Unknown Wiz environment '{env}'. Valid options are: app, gov, fedramp."
            )
        if not cls.domain_enabled(env):
            raise WizConfigurationError(
                f"Wiz environment '{env}' is disabled in config. "
                f"Enable domain.{env}.enabled to use it."
            )
        return root

    @classmethod
    @ensure_loaded
    def domain_root(cls, env: str) -> Optional[str]:
        """Return the root domain string for the given environment identifier."""
        root_domains = {
            "app": "app.wiz.io",
            "gov": "gov.wiz.io",
            "fedramp": "app.wiz.us",
        }
        return root_domains.get(env)

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
    def api_auto_paginate(cls) -> bool:
        """Return whether requests should paginate by default."""
        return cls.get("api", "auto_paginate", default=True)

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

    ############
    # Rate limiting
    ############
    @classmethod
    @ensure_loaded
    def rate_limit_headroom(cls) -> float:
        """Return the fraction of Wiz's published rate limits to use locally.

        Running below the published limits (default 0.8) leaves headroom for
        network jitter and other consumers of the same tenant quota, keeping
        normal operation clear of server-side 429s. Values above 1.0 are
        honored but risk tripping the server limiter. Invalid or non-positive
        values fall back to the default.
        """
        try:
            headroom = float(cls.get("rate_limit", "headroom", default=0.8))
        except (TypeError, ValueError):
            return 0.8
        if headroom <= 0:
            return 0.8
        return headroom

    @classmethod
    @ensure_loaded
    def rate_limit_overrides(cls) -> Dict[str, float]:
        """Return explicit requests-per-second overrides per limiter key.

        Keys are limiter names (query_user, query_service, mutation_user,
        mutation_service); values are requests per second. An override wins
        over the headroom-scaled published limit for that key. Invalid or
        non-positive values are ignored.
        """
        overrides = cls.get("rate_limit", "overrides", default={}) or {}
        if not isinstance(overrides, dict):
            return {}
        result: Dict[str, float] = {}
        for key, value in overrides.items():
            try:
                rps = float(value)
            except (TypeError, ValueError):
                continue
            if rps > 0:
                result[str(key)] = rps
        return result

    @classmethod
    @ensure_loaded
    def rate_limit_max_backoff_waits(cls) -> int:
        """Return how many consecutive server rate-limit waits a request
        tolerates before giving up. These waits do not consume API retry
        attempts."""
        try:
            waits = int(cls.get("rate_limit", "max_backoff_waits", default=10))
        except (TypeError, ValueError):
            return 10
        return waits if waits > 0 else 10

    @classmethod
    @ensure_loaded
    def rate_limit_default_retry_after(cls) -> int:
        """Return the backoff in seconds used when the server rate-limits
        without a usable Retry-After value (missing, non-integer, or a
        GraphQL-level rate-limit error inside a 200 response)."""
        try:
            seconds = int(cls.get("rate_limit", "default_retry_after", default=10))
        except (TypeError, ValueError):
            return 10
        return seconds if seconds > 0 else 10


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
