##
##
##   W       W  IIIIIII  ZZZZZZZ   SSSSS   EEEEE    CCCCC
##   W       W     I         ZZ   S       E       C
##   W   W   W     I       ZZ     SSSSS   EEEEE   C
##    W WWW W      I      ZZ          S   E       C
##     W   W    IIIIIII  ZZZZZZZ  SSSSS   EEEEE    CCCCC
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wizsec
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
    # Schema
    "SchemaValidator",
    # Exceptions
    "WizError",
    "WizAuthenticationError",
    "WizAPIError",
    "WizConfigurationError",
    "WizCredentialsError",
    "WizRateLimitError",
    "WizQueryError",
    "WizSchemaValidationError",
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
    elif name == "SchemaValidator":
        from ._schema import SchemaValidator
    elif name in (
        "WizError",
        "WizAuthenticationError",
        "WizAPIError",
        "WizConfigurationError",
        "WizCredentialsError",
        "WizRateLimitError",
        "WizQueryError",
        "WizSchemaValidationError",
        "WizTimeoutError",
        "WizFileError",
        "WizServerlessError",
    ):
        from .exceptions import (
            WizError,
            WizAuthenticationError,
            WizAPIError,
            WizConfigurationError,
            WizCredentialsError,
            WizRateLimitError,
            WizQueryError,
            WizSchemaValidationError,
            WizTimeoutError,
            WizFileError,
            WizServerlessError,
        )
    else:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    return locals()[name]
