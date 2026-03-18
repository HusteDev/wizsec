##
## 
##   CCCCC    OOOOO   N     N   FFFFFF   IIIIIII   GGGGG
##  C        O     O  NN    N   F           I     G     
##  C        O     O  N N   N   FFFFF       I     G   GGG
##  C        O     O  N  N  N   F           I     G     G
##   CCCCC    OOOOO   N     N   F        IIIIIII   GGGGG
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wiz-sdk
##

# config.py
from pathlib import Path
import yaml
import sys
import os
import logging
import tempfile
import inspect
from functools import wraps
from datetime import date, datetime
from typing import Optional, Union
try:
    from importlib.metadata import version as get_version
except ImportError:
    # Python <3.8 fallback (not typical in modern environments, but just in case)
    from importlib_metadata import version as get_version

from wiz_sdk.version import __version__ as wiz_sdk_version
from wiz_sdk.version import __sdk_name__ as sdk_name

LIBRARY_NAME = sdk_name
CURRENT_VERSION = wiz_sdk_version

# _CONFIG = None
SERVERLESS = str(os.environ.get("WIZ_SERVERLESS", "")).lower() in ("1", "true", "yes") or bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DEFAULT_WIZ_DIR = Path("/var/task/.wiz") if SERVERLESS else Path.home() / ".wiz"
DEFAULT_TEMP_FOLDER = "/tmp" if SERVERLESS else Path(tempfile.gettempdir())
CWD = "/tmp" if SERVERLESS else str(os.getcwd()).replace("\\", "/")
VERBOSE_LEVEL = 15  # Between INFO (20) and DEBUG (10)
BASE_LOGGER_NAME = "wiz_sdk"

class Config:
    _CONFIG = None
    _logger = None
    _loaded = False

    @classmethod
    def load(cls, config_path: str = None, overrides: dict = None,
             client_id: str = None, client_secret: str = None):
        if cls._CONFIG is None:
            config_path = config_path or str(DEFAULT_WIZ_DIR / "wiz.config")
            path, filename = parse_filepath(str(config_path))
            if  all([path, path.is_dir(), os.path.exists(path / filename), filename, isinstance(filename, str)]):
                with open(path / filename, "r") as file:
                    cls._CONFIG = yaml.safe_load(file)
            else:
                if not SERVERLESS:
                    created = generate_default_config(str(config_path))
                    if created:
                        with open(str(config_path), "r") as file:
                            cls._CONFIG = yaml.safe_load(file)
                else:
                    raise Exception("Create config file at /var/task/.wiz for all Serverless executions")

            # Apply overrides via dot-notation keys
            for item in (overrides or {}):
                if isinstance(item, str) and '=' in item:
                    key, value = item.split('=', 1)
                    value = yaml.safe_load(value)
                elif isinstance(item, str):
                    continue
                else:
                    continue
                keys = key.split('.')
                d = cls._CONFIG
                for k in keys[:-1]:
                    if k not in d or not isinstance(d[k], dict):
                        d[k] = {}
                    d = d[k]
                d[keys[-1]] = value

            if client_id:
                os.environ['WIZ_CLIENT_ID'] = client_id
            if client_secret:
                os.environ['WIZ_CLIENT_SECRET'] = client_secret

            if not cls.serverless():
                config_version = cls._CONFIG.get("app", {}).get("release")
                try:
                    library_version = get_version("wiz_sdk")
                    if config_version != library_version:
                        sys.stdout.write(f"Warning: Config file version [{config_version}] doesn't match the installed library version [{library_version}]\n")
                        sys.stdout.flush()
                except Exception as e:
                    sys.stdout.write(f"Error checking library version: {str(e)}\n")
                    sys.stdout.flush()

                # CA Certificate
                ca_file_path, filename = parse_filepath(cls._CONFIG.get("auth", {}).get("ca_cert", ""))
                if ca_file_path and filename:
                    cert_path = str(Path(ca_file_path) / filename)
                    os.environ['REQUESTS_CA_BUNDLE'] = cert_path
                else:
                    ca_env_vars = [
                        'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE', 'PIP_CERT',
                        'AWS_CA_BUNDLE', 'GIT_SSL_CAINFO', 'NODE_EXTRA_CA_CERTS'
                    ]
                    existing_cert = None
                    for var in ca_env_vars:
                        val = os.environ.get(var)
                        if val:
                            existing_cert = val
                            break
                    if existing_cert:
                        os.environ['REQUESTS_CA_BUNDLE'] = existing_cert
        cls._loaded = True
        if cls._logger is None:
            cls._logger = logging_init(name="wiz_sdk")

    def ensure_loaded(func):
        @wraps(func)
        def wrapper(cls, *args, **kwargs):
            if not cls._loaded:
                cls.load()
            return func(cls, *args, **kwargs)
        return wrapper
            

    @classmethod
    @ensure_loaded
    def get_logger(cls) -> logging.Logger:
        base_logger = cls._logger or logging_init()

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
            logger.propagate = False                     # don’t bubble to root
            logger._baselogger_initialized = True
        else:
            # keep level in sync with base, in case config changed on a warm start
            logger.setLevel(base_logger.level)

        return logger
    
    @classmethod
    @ensure_loaded
    def load_dotenv(cls):
        if cls.serverless():
            return  # skip loading files in serverless mode
        from dotenv import load_dotenv
        env_path, _ = parse_filepath(cls.env_path())
        dotenv_file = str(env_path / '.env')
        if os.path.exists(dotenv_file):
            load_dotenv(dotenv_path=dotenv_file)

    @classmethod
    @ensure_loaded
    def get(cls, *keys, default=None):
        d = cls._CONFIG
        for key in keys:
            d = d.get(key, {}) if isinstance(d, dict) else {}
        result = d if d else default
        if cls._logger:
            cls.get_logger().debug(f"get() called with keys={keys} -> {result}")
        return result

    @classmethod
    @ensure_loaded
    def set_log_level(
            cls,
            level: Union[int, str],
            *,
            handler_level: Optional[Union[int, str]] = None,
            include_children: bool = True
        ) -> int:
        """
        Change the library logging level at runtime.

        - `level`: logger level for base and children (e.g. 'DEBUG', 20, 'VERBOSE')
        - `handler_level`: optional handler threshold; defaults to `level`
        - `include_children`: also retune wiz_sdk.<module> child loggers

        Returns the effective base level as an int.
        """
        # Make sure base logger exists
        base_logger = cls._logger or logging_init()
        base_logger_name = getattr(base_logger, "name", BASE_LOGGER_NAME)

        new_level = _parse_level(level, default=logging.INFO)
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
    def app_name(cls):
        return cls.get("app", "name", default="wiz_sdk")

    @classmethod
    @ensure_loaded
    def release_version(cls):
        return cls.get("app", "release", default="0.0.0")

    @classmethod
    def serverless(cls):
        return SERVERLESS
    
    @classmethod
    @ensure_loaded
    def wiz_dir(cls):
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath( cls.get("app", "wiz_dir", default=DEFAULT_WIZ_DIR))
        cls.get_logger().debug(f"Resolved wiz_dir: {filepath}")
        return filepath
    
    @classmethod
    @ensure_loaded
    def env_path(cls):
        filepath, _ = parse_filepath( cls.get("app", "env_path", default=f"{CWD}"))
        cls.get_logger().debug(f"Resolved env_path: {filepath}")
        return filepath

    ############
    # Save
    ############
    @classmethod
    @ensure_loaded
    def saved_data_enabled(cls):
        if cls.serverless():
            return False
        return cls.get("saved_data", "enabled", default=True)

    @classmethod
    @ensure_loaded
    def saved_data_directory(cls):
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(cls.get("saved_data", "directory", default=f"{CWD}/saved-data"))
        return filepath

    @classmethod
    @ensure_loaded
    def saved_data_temp_directory(cls): 
        if cls.serverless():
            return Path("/tmp")
        filepath, _ = parse_filepath(cls.get("saved_data", "temp", default=DEFAULT_TEMP_FOLDER))
        return filepath

    @classmethod
    @ensure_loaded
    def saved_data_pickle_enabled(cls): 
        if cls.serverless():
            return False
        return cls.get("saved_data", "pickle", default=False)

    ############
    # Auth
    ############
    @classmethod
    @ensure_loaded
    def grant_type(cls):
        if cls.serverless():
            return "client_credentials"
        return cls.get("auth", "grant_type", default="client_credentials")
    
    @classmethod
    @ensure_loaded
    def storage_method(cls):
        return cls.get("auth", "credentials", "storage_method", default="file")
    
    @classmethod
    @ensure_loaded
    def credential_file_path(cls):
        filepath, _ = parse_filepath(cls.get("auth", "credentials", "file_path", default=f"{DEFAULT_WIZ_DIR}"))
        cls.get_logger().debug(f"Resolved credential file path: {filepath}")
        return filepath

    @classmethod
    @ensure_loaded
    def ca_cert(cls):
        return cls.get("auth", "ca_cert", default="")
    
    @classmethod
    @ensure_loaded
    def quiet_auth(cls):
        return cls.get("auth", "device", "quiet", default="true")
    
    @classmethod
    @ensure_loaded
    def auth_poll_time(cls):
        return int(cls.get("auth", "device", "poll_time", default=5))

    @classmethod
    @ensure_loaded
    def get_proxies(cls):
        if cls.serverless():
            return {"https": os.environ.get("HTTPS_PROXY")}
        proxy_config = cls.get("auth", "proxy", default={})
        return {
            "http": (
                f'{proxy_config.get("http", {}).get("url", "")}:{proxy_config.get("http", {}).get("port", "")}'
                if proxy_config else os.environ.get("HTTP_PROXY", None)
            ),
            "https": (
                f'{proxy_config.get("https", {}).get("url", "")}:{proxy_config.get("https", {}).get("port", "")}'
                if proxy_config else os.environ.get("HTTPS_PROXY", None)
            ),
        }

    ############
    # Domain
    ############
    @classmethod
    @ensure_loaded
    def default_domain(cls):
        return cls.get("domain", "default", default="gov")
    
    @classmethod
    @ensure_loaded
    def domain_enabled(cls, domain):
        return cls.get("domain", domain, "enabled", default=False)

    @classmethod
    @ensure_loaded
    def domain_root(cls, env):
        root_domains = {
            "app"       :   "app.wiz.io",
            "gov"       :   "gov.wiz.io",
            "fedramp"   :   "app.wiz.us"
            }
        return root_domains.get(env, root_domains.get(cls.default_domain()))

    ############
    # API
    ############
    @classmethod
    @ensure_loaded
    def api_max_retries(cls):
        return cls.get("api", "max_retries", default=5)
    
    @classmethod
    @ensure_loaded
    def api_retry(cls):
        return cls.get("api", "retry_time", default=2)

    @classmethod
    @ensure_loaded
    def api_timeout(cls):
        return cls.get("api", "timeout", default=180)

    ############
    # Reports
    ############
    @classmethod
    @ensure_loaded
    def report_stream_by_default(cls):
        return cls.get("reports", "stream_by_default", default=True)

    @classmethod
    @ensure_loaded
    def report_export_directory(cls):
        filepath, _ = parse_filepath(cls.get("reports", "export_directory", default=f"{CWD}"))
        cls.get_logger().debug(f"Resolved report export directory: {filepath}")
        return filepath

    @classmethod
    @ensure_loaded
    def report_export_type(cls):
        return cls.get("reports", "export_type", default="json")

    @classmethod
    @ensure_loaded
    def report_retry_time(cls):
        return cls.get("reports", "retry_time", default=30)

    @classmethod
    @ensure_loaded
    def report_max_retries(cls):
        return cls.get("reports", "max_retries", default=3)

    @classmethod
    @ensure_loaded
    def report_polling_time(cls):
        return cls.get("reports", "polling_time", default=15)

    @classmethod
    @ensure_loaded
    def report_auto_cleanup(cls):
        return cls.get("reports", "auto_cleanup", default=False)

    @classmethod
    @ensure_loaded
    def report_save_incomplete(cls):
        return cls.get("reports", "save_incomplete_reports", default=True)

    ############
    # Logging
    ############
    @classmethod
    @ensure_loaded
    def logging_enabled(cls):
        val = cls.get("logging", "enabled", default=True)
        return val
    
    @classmethod
    @ensure_loaded
    def logger_min_level(cls):
        val = cls.get("logging", "lowest_level", default=10)
        return val

    @classmethod
    @ensure_loaded
    def verbose_mode(cls):
        val = cls.get("logging", "verbose", default=False)
        return val

    @classmethod
    @ensure_loaded
    def debug_mode(cls):
        val = cls.get("logging", "debug", default=False)
        return val

    @classmethod
    @ensure_loaded
    def file_logging_enabled(cls):
        if cls.serverless():
            return False
        val = cls.get("logging", "file_handler", "enabled", default=False)
        return val
    
    @classmethod
    @ensure_loaded
    def file_handler_logging_level(cls):
        return cls.get("logging", "file_handler", "logging_level", default="DEBUG")

    @classmethod
    @ensure_loaded
    def file_handler_log_directory(cls):
        if cls.serverless():
            return "/tmp"
        path_str = cls.get("logging", "file_handler", "log_directory", default="")
        if not path_str: # if empty string from config
            return DEFAULT_WIZ_DIR
        filepath, _ = parse_filepath(path_str)
        return filepath

    @classmethod
    @ensure_loaded
    def file_handler_create_log_dir(cls):
        return cls.get("logging", "file_handler", "create_log_dir", default=True)

    @classmethod
    @ensure_loaded
    def file_handler_markdown_enabled(cls):
        return cls.get("logging", "file_handler", "markdown", default=False)

    @classmethod
    @ensure_loaded
    def console_handler_enabled(cls):
        # Assuming console logging is always enabled if main logging is, unless specified otherwise
        return cls.get("logging", "console_handler", "enabled", default=cls.logging_enabled())

    @classmethod
    @ensure_loaded
    def console_handler_logging_level(cls):
        return cls.get("logging", "console_handler", "logging_level", default="INFO")


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
        
        # Wrap the message in markdown table row format
        markdown_message = formatted_message
        
        # Add header if not already added
        if not MarkdownFormatter.header_written:
            header = (
                '| Timestamp | Level | File | Function | Line | Thread ID | Thread Name | Message |\n'
                '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
            )
            MarkdownFormatter.header_written = True
            markdown_message = header + markdown_message
        
        return markdown_message

def generate_default_config(file_path=(DEFAULT_WIZ_DIR / "wiz.config")):
    if SERVERLESS:
        return False
    wiz_dir = str(DEFAULT_WIZ_DIR).replace("\\", "/")
    yaml_content = f'''app:
  name: {LIBRARY_NAME}
  release: {CURRENT_VERSION}
 # serverless: false                 # Enables additional functions to support Serverless execution | default = false
 # wiz_dir: ""      # Default directory for Wiz config, auth, and logging files
 # env_path: ""       # Custom directory for .env file | default = $CWD

# saved_data:
  # enabled: true                    # Allows application save data locally | default = true
  # directory: ""    # Custom directory to save non-report exports | default = $CWD/saved-data
  # temp: ""                        # Custom directory for Temp Files | default = tempfile.gettempdir()
  # pickle: false                   # Allow use of pickle files for storing larger files | default = false

# auth:
  # grant_type: client_credentials    # Options [device_code, client_credentials] | NOTE: device_code requires WizCode license
#   credentials:
#     storage_method: file            # Options [env, file, prompt] | default = file (which will also check .env if no file found)
 #   file_path: ""  # Custom directory for credentials | default = $HOME/.wiz/

#   device:
#     quiet: true                     # Device Code logins automatically authorize on load | default = true
#     poll_time: 5                    # Interval (in seconds) for checking if device auth is complete | default = 5
  # proxy:                          # Custom Proxy Settings if necessary | default = use system settings
  #   http: 
  #     url: ""
  #     port: 80
  #   https: 
  #     url: ""
  #     port: 80
  # ca_cert: ""                     # Path to Custom CA (.pem) file | example: "/cacert.pem"

# domain:                             # Domains not enabled will be blocked from Authentication
  # default: gov                      # Valid Options [app, gov, fedramp] | default = gov
  # app:
    # enabled: false
  # gov:
    # enabled: true
  # fedramp:
    # enabled: false

# api:
#   max_retries: 5                    # Max number of failures before query stops trying | default = 5
#   retry_time: 1                     # Initial wait time for query retry attempts | default = 1
#   timeout: 120                       # Wiz API queries timeout at 2 min, this just keeps the program from retrying
  # auto_paginate: true               # Automatically paginate through results | default = true

# reports:
#   stream_by_default: true             # Streaming reports download reports automatically | default = true
#  export_directory: ""                # Path to save report export files | default = working_directory
#   export_type: json                   # Valid Options [json, csv]
#   retry_time: 30                      # Time between attempts to generate a new report | default = 30
#   max_retries: 3                      # Max attempts to run report before reporting failure | default = 3
#   polling_time: 15                    # Time between attempts to check report status | default = 15
#   auto_cleanup: false                 # Removes any reports sent to /temp. Deletes old Reports from Wiz 
#   save_incomplete_reports: true       # Save partial reports if at least 1 page is successfully retrieved before error

# logging:
#   enabled: false                    # Enables logging for library | default = false
#   verbose: false                    # Enable VERBOSE messages | default = false
#   lowest_level: 10                  # If higher than 10, debug messages will not process for any handler | default = 10
#   debug: false                      # Enable DEBUG messages | default = false
#   file_handler:                     
#     enabled: false                  # Enables logging to a file | default = false
#     logging_level: DEBUG            # Log level for file handler. | default = DEBUG
#     log_directory: ""               # Custom Filepath to save Log Files | default = "$HOME/.wiz" (adds /logs/ folder)
#     create_log_dir: true            # Automatically create the log directory if it does not exist | default = true
#     markdown: false                 # Use Markdown format to put log entries into table format | default = false
#   console_handler:
#     enabled: true                   # Enables logging to console (stdout) | default = true
#     logging_level: INFO             # Log level for console handler. | default = INFO
'''

    if not DEFAULT_WIZ_DIR.exists():
        DEFAULT_WIZ_DIR.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w") as file:
        file.write(yaml_content)

    if not os.path.exists(DEFAULT_WIZ_DIR / "wiz.credentials"):
        Path(str(DEFAULT_WIZ_DIR / "wiz.credentials")).touch()

    sys.stdout.write(f"New config file created at {file_path}\n")
    sys.stdout.flush()

    return True


'''def _build_library_logger_for_lambda(level: int) -> logging.Logger:
    """
    Dedicated logger for Lambda: its own StreamHandler to stdout,
    explicit level, no propagation to root (to avoid root's WARNING filter),
    and an idempotent init so we don't duplicate handlers on warm starts.
    """

    logger = logging.getLogger(BASE_LOGGER_NAME)

    if getattr(logger, "_baselogger_initialized", False):
        logger.setLevel(level)
        return logger

    logger.setLevel(level)
    logger.propagate = False

    h = logging.StreamHandler(sys.stdout)
    h.setLevel(level)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    h.setFormatter(logging.Formatter(fmt))

    logger.handlers.clear()        # own handler, independent of root
    logger.addHandler(h)

    logger._baselogger_initialized = True
    return logger


def _build_library_logger_for_local(level: int) -> logging.Logger:
    """
    Non-serverless: still create a dedicated base logger.
    If you also want file logging, you can add a FileHandler here behind your existing flags.
    """

    logger = logging.getLogger(BASE_LOGGER_NAME)
    if getattr(logger, "_baselogger_initialized", False):
        logger.setLevel(level)
        return logger
    
    if not Config.logging_enabled():
        logger.addHandler(logging.NullHandler())  # No-op logger, ignores all log calls
        return logger

    logger.setLevel(level)
    logger.propagate = False


    if Config.console_handler_enabled():
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_log_level_str = Config.console_handler_logging_level().upper()
        console_log_level = getattr(logging, console_log_level_str, Config.logger_min_level())
        # Override with verbose if enabled and higher precedence
        if Config.verbose_mode() and console_log_level > logging.VERBOSE:
            console_log_level = logging.VERBOSE
        console_handler.setLevel(console_log_level)

        date_format = '%H:%M:%S'
        console_log_format = '\r%(asctime)s [%(name)s::%(levelname)s]:   %(message)s\033[K'
        console_formatter = logging.Formatter(fmt=console_log_format, datefmt=date_format)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        logger.console_handler = console_handler

    if Config.file_logging_enabled() and not Config.serverless():
        log_path_from_config_str = Config.get("logging", "file_handler", "log_directory", default="")
        if not log_path_from_config_str: # If empty, use DEFAULT_WIZ_DIR
            log_path_from_config = DEFAULT_WIZ_DIR
        else:
            log_path_from_config, _ = parse_filepath(log_path_from_config_str)

        log_directory = log_path_from_config / 'logs'
        if Config.file_handler_create_log_dir() and not log_directory.is_dir():
            log_directory.mkdir(parents=True, exist_ok=True)

        log_filename = f'{date.today().strftime("%Y%m%d")}_{datetime.now().strftime("%H%M%S")}_{Path(sys.argv[0]).stem}.log'        
        log_path = log_directory / log_filename

        logger.log_directory = log_directory

        file_handler = logging.FileHandler(filename=log_path, mode="w")
        file_log_level_str = Config.file_handler_logging_level().upper()
        file_log_level = getattr(logging, file_log_level_str, logging.DEBUG)
        file_handler.setLevel(file_log_level)

        date_format = '%d-%b-%y %H:%M:%S.%f'
        if Config.file_handler_markdown_enabled():    
            file_log_format = '| %(asctime)s | %(levelname)s | %(filename)s | %(funcName)s | %(lineno)d | %(thread)d | %(threadName)s | %(message)s |'
            file_formatter = MarkdownFormatter(fmt=file_log_format, datefmt=date_format)
        else:
            file_log_format = '%(asctime)s  [%(levelname)s] - (%(name)s):  %(message)s'
            file_formatter = CustomFormatter(fmt=file_log_format, datefmt=date_format)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        logger.file_handler = file_handler

    logger._baselogger_initialized = True
    return logger


def logging_init(name: str = BASE_LOGGER_NAME, default_level=logging.INFO) -> logging.Logger:

    if not hasattr(logging, "VERBOSE"):
        logging.addLevelName(VERBOSE_LEVEL, "VERBOSE")

        def _verbose(self, message, *args, **kwargs):
            if self.isEnabledFor(VERBOSE_LEVEL):
                self._log(VERBOSE_LEVEL, message, args, **kwargs)

        logging.Logger.verbose = _verbose
        logging.VERBOSE = VERBOSE_LEVEL

    try:
        cfg_level = Config.logger_min_level()
        level = int(cfg_level) if isinstance(cfg_level, int) else getattr(logging, str(cfg_level).upper(), logging.INFO)
    except Exception:
        level = default_level

    if Config.serverless():
        return _build_library_logger_for_lambda(level)
    else:
        return _build_library_logger_for_local(level)
'''

def _ensure_verbose_console_level(level: int) -> int:
    # If verbose mode on, prefer VERBOSE over a higher level
    if Config.verbose_mode() and level > logging.VERBOSE:
        return logging.VERBOSE
    return level


def _init_lambda_logger(level: int) -> logging.Logger:
    # Keep lambda path minimal and non-propagating with a clean stdout format
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


def _maybe_attach_console_handler(logger: logging.Logger) -> None:
    if not Config.console_handler_enabled():
        return
    lvl_name = str(Config.console_handler_logging_level()).upper()
    lvl = getattr(logging, lvl_name, Config.logger_min_level())
    lvl = _ensure_verbose_console_level(lvl)

    h = logging.StreamHandler(stream=sys.stdout)
    h.setLevel(lvl)
    fmt = '\r%(asctime)s [%(name)s::%(levelname)s]:   %(message)s\033[K'
    h.setFormatter(logging.Formatter(fmt=fmt, datefmt='%H:%M:%S'))
    logger.addHandler(h)
    logger.console_handler = h  # parity with your original


def _maybe_attach_file_handler(logger: logging.Logger) -> None:
    if not (Config.file_logging_enabled() and not Config.serverless()):
        return

    cfg_dir = Config.get("logging", "file_handler", "log_directory", default="")
    base_dir = DEFAULT_WIZ_DIR if not cfg_dir else parse_filepath(cfg_dir)[0]
    log_dir = base_dir / "logs"

    if Config.file_handler_create_log_dir() and not log_dir.is_dir():
        log_dir.mkdir(parents=True, exist_ok=True)

    fname = f'{date.today():%Y%m%d}_{datetime.now():%H%M%S}_{Path(sys.argv[0]).stem}.log'
    path = log_dir / fname

    logger.log_directory = log_dir

    h = logging.FileHandler(filename=path, mode="w")
    lvl_name = str(Config.file_handler_logging_level()).upper()
    lvl = getattr(logging, lvl_name, logging.DEBUG)
    h.setLevel(lvl)

    datefmt = '%d-%b-%y %H:%M:%S.%f'
    if Config.file_handler_markdown_enabled():
        fmt = '| %(asctime)s | %(levelname)s | %(filename)s | %(funcName)s | %(lineno)d | %(thread)d | %(threadName)s | %(message)s |'
        h.setFormatter(MarkdownFormatter(fmt=fmt, datefmt=datefmt))
    else:
        fmt = '%(asctime)s  [%(levelname)s] - (%(name)s):  %(message)s'
        h.setFormatter(CustomFormatter(fmt=fmt, datefmt=datefmt))

    logger.addHandler(h)
    logger.file_handler = h  # parity


def logging_init(name: str = __name__, default_level=None) -> logging.Logger:

    if not hasattr(logging, "VERBOSE"):
        logging.addLevelName(VERBOSE_LEVEL, "VERBOSE")

        def _verbose(self, message, *args, **kwargs):
            if self.isEnabledFor(VERBOSE_LEVEL):
                self._log(VERBOSE_LEVEL, message, args, **kwargs)

        logging.Logger.verbose = _verbose
        logging.VERBOSE = VERBOSE_LEVEL

    # Resolve base level from config
    try:
        cfg_level = Config.logger_min_level()
        base_level = int(cfg_level) if isinstance(cfg_level, int) else getattr(logging, str(cfg_level).upper(), logging.INFO)
    except Exception:
        base_level = logging.INFO

    # Lambda path
    if Config.serverless():
        return _init_lambda_logger(base_level)

    # Use requested logger name, but respect pre-configured loggers
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        # Caller already configured; keep their setup
        return logger

    # Otherwise use the library logger to centralize behavior
    logger = logging.getLogger(BASE_LOGGER_NAME)

    if not Config.logging_enabled():
        # No-op logger
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
        return logger

    # Clean slate init only once
    if not getattr(logger, "_baselogger_initialized", False):
        logger.handlers.clear()
        logger.setLevel(base_level)
        logger.propagate = False
        _maybe_attach_console_handler(logger)
        _maybe_attach_file_handler(logger)
        logger._baselogger_initialized = True
    else:
        # If re-entering, at least honor level changes
        logger.setLevel(base_level)

    logger.verbose(
        "Logging enabled: %s | Root Level: %s | Debug Mode: %s | Verbose Logging: %s | File Logging: %s",
        Config.logging_enabled(), Config.logger_min_level(), Config.debug_mode(),
        Config.verbose_mode(), Config.file_logging_enabled()
    )
    return logger

def _parse_level(level: Union[int, str, None], default=logging.INFO) -> int:
    if level is None:
        return default
    if isinstance(level, int):
        return level
    try:
        # allow "15" as string too
        if str(level).isdigit():
            return int(level)
        name = str(level).upper()
        if name == "VERBOSE":
            return getattr(logging, "VERBOSE", 15)
        return getattr(logging, name, default)
    except Exception:
        return default

def expand_all_vars(path_str: str) -> str:
    path_str = str(path_str)  # ensure it's a string
    path_str = path_str.replace("$CWD", str(Path.cwd()))
    path_str = path_str.replace("$HOME", str(Path.home()))
    expanded = os.path.expanduser(path_str)
    expanded = os.path.expandvars(expanded)
    return expanded

def parse_filepath(path_str: str = None) -> Path:
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
    