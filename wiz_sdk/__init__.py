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
    "WizClient", 
    "dump_to_json", 
    "Config", 
    "load_file_if_in_last_x_interval",
    "WizBatchRequest",
    "WizBatchResponse",
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
    "WizServerlessError"
]

def __getattr__(name):
    if name == "WizClient":
        from .client import WizClient
    elif name == "dump_to_json":
        from wiz_sdk.utils import dump_to_json
    elif name == "load_file_if_in_last_x_interval":
        from wiz_sdk.utils import load_file_if_in_last_x_interval
    elif name == "Config":
        from wiz_sdk.config import Config
    elif name in ["WizBatchRequest", "WizBatchResponse"]:
        from wiz_sdk._request import WizBatchRequest, WizBatchResponse
    elif name in [
        "WizError", "WizAuthenticationError", "WizAPIError", "WizConfigurationError",
        "WizCredentialsError", "WizRateLimitError", "WizQueryError", "WizReportError",
        "WizTimeoutError", "WizFileError", "WizServerlessError"
    ]:
        from wiz_sdk.exceptions import (
            WizError, WizAuthenticationError, WizAPIError, WizConfigurationError,
            WizCredentialsError, WizRateLimitError, WizQueryError, WizReportError,
            WizTimeoutError, WizFileError, WizServerlessError
        )
    else:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    return locals()[name]
