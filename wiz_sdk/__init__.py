##
## 
##   WW       WW  IIIIIII  ZZZZZZZ       SSSSS    DDDDD    K   K
##   WW       WW     I         ZZ       SS        D    D   K  K
##   WW   W   WW     I       ZZ           SSSS    D    D   KKK
##    WW WWW WW      I      ZZ               SS   D    D   K  K
##     WW   WW    IIIIIII  ZZZZZZZ_______SSSSS    DDDDD    K   K
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wiz-sdk
##

# __init__.py

__all__ = [
    # Core
    "WizClient",
    "Config",
    # Request / Response
    "WizBatchRequest",
    "WizBatchResponse",
    "AsyncWizBatchRequest",
    # Utilities
    "dump_to_json",
    "load_file_if_in_last_x_interval",
    # Registries
    "EnvironmentRegistry",
    "ProfileRegistry",
    # Exceptions
    "WizError",
    "WizAuthenticationError",
    "WizAPIError",
    "WizConfigurationError",
    "WizCredentialsError",
    "WizRateLimitError",
    "WizQueryError",
    "WizReportError",
    "WizTimeoutError",
    "WizFileError",
    "WizServerlessError",
]

def __getattr__(name):
    if name == "WizClient":
        from .client import WizClient
    elif name == "Config":
        from .config import Config
    elif name in ("WizBatchRequest", "WizBatchResponse", "AsyncWizBatchRequest"):
        from ._request import WizBatchRequest, WizBatchResponse, AsyncWizBatchRequest
    elif name == "dump_to_json":
        from .utils import dump_to_json
    elif name == "load_file_if_in_last_x_interval":
        from .utils import load_file_if_in_last_x_interval
    elif name in ("EnvironmentRegistry", "ProfileRegistry"):
        from ._registry import EnvironmentRegistry, ProfileRegistry
    elif name in (
        "WizError", "WizAuthenticationError", "WizAPIError", "WizConfigurationError",
        "WizCredentialsError", "WizRateLimitError", "WizQueryError", "WizReportError",
        "WizTimeoutError", "WizFileError", "WizServerlessError",
    ):
        from .exceptions import (
            WizError, WizAuthenticationError, WizAPIError, WizConfigurationError,
            WizCredentialsError, WizRateLimitError, WizQueryError, WizReportError,
            WizTimeoutError, WizFileError, WizServerlessError,
        )
    else:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    return locals()[name]
