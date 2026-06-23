##
##
##   U     U TTTTTTT IIIIIII  L        SSSSSSS
##   U     U    T       I     L        S
##   U     U    T       I     L        SSSSSSS
##   U     U    T       I     L              S
##    UUUUU     T    IIIIIII  LLLLLLL  SSSSSSS
##
##
##

# utils.py
import sys
import json
import csv
import copy
import shutil
import traceback
import time
import itertools
import os
import logging
import configparser
import mimetypes
from graphql import parse
from graphql.language import print_ast
from graphql.language.ast import (
    FieldNode,
    OperationDefinitionNode,
    VariableDefinitionNode,
    NamedTypeNode,
    NameNode,
    ArgumentNode,
    VariableNode,
)
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from functools import wraps
from typing import Callable, Optional, Dict, Any, List, Union, Tuple
from .config import Config, parse_filepath, DEFAULT_TEMP_FOLDER
from ._logging import logging_init

import pickle

mimetypes.add_type("application/python-pickle", ".pkl")
mimetypes.add_type("application/python-pickle", ".pickle")

from .exceptions import WizFileError, WizConfigurationError, WizCacheError
from ._cache import (
    CacheBackend,
    get_configured_backend,
    safe_write_json,
    find_file_with_extension,
)


def disable_in_serverless(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that skips the wrapped function when running in serverless mode."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if Config.serverless():
            Config.get_logger().debug(f"{func.__name__} disabled in serverless mode.")
            return None  # or raise, or return original input
        return func(*args, **kwargs)

    return wrapper


def resource_path(relative_path: str) -> str:
    """Get the absolute path to the resource."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


def dump_to_json(
    data: Any,
    filename: Optional[str] = None,
    filepath: Optional[str] = None,
    retry: bool = True,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Serialize data to a JSON (or pickle) file."""
    logger = Config.get_logger()
    try:
        logger.verbose("Attempting to dump_to_json")
    except AttributeError:
        if logger is None:
            from .config import DEFAULT_WIZ_DIR

            logger = logging_init(Config, DEFAULT_WIZ_DIR, parse_filepath)
    if data:
        allow_pickle = Config.cache_pickle_enabled()

        configs_save, _ = parse_filepath(Config.cache_directory())
        save_directory, extracted_filename = (
            parse_filepath(filepath) if filepath else (configs_save, None)
        )
        if not save_directory.exists():
            save_directory.mkdir(parents=True, exist_ok=True)

        temp_directory = Path(DEFAULT_TEMP_FOLDER)
        if not temp_directory.exists():
            temp_directory.mkdir(parents=True, exist_ok=True)

        if save_directory.is_file() and filename:
            save_directory = save_directory.parent

        if filename and not filepath:
            save_directory = configs_save

        file_name = filename if filename else extracted_filename
        if not file_name:
            file_name = "dumpFile.json"

        basename = Path(file_name).stem

        start = time.perf_counter()
        while True:
            try:
                if (
                    Path(file_name).suffix == ".json"
                    or sys.getsizeof(data) <= 100000
                    or not allow_pickle
                ):
                    result: Any = None
                    if isinstance(data, (list, tuple)):
                        if all([isinstance(row, list) for row in data]) and (
                            len(data[0]) < len(set(data[0]))
                        ):
                            header_count: Dict[Any, int] = {}
                            unique_headers = [
                                (
                                    header
                                    if header_count.setdefault(header, 0) == 0
                                    else f"{header}_{header_count[header]}"
                                )
                                for header in data[0]
                                for header_count[header] in [
                                    header_count.get(header, 0) + 1
                                ]
                            ]
                            result = [
                                dict(
                                    itertools.zip_longest(
                                        unique_headers, row, fillvalue=None
                                    )
                                )
                                for row in data[1:]
                            ]
                        data = {"data": result if result else data}
                    safe_write_json(data, file_name, save_directory, temp_directory)
                    logger.verbose("%s.json created", basename)
                else:
                    if allow_pickle:
                        logger.verbose("Object too large, saving as pickle instead")
                        data = store_data(data, save_directory / f"{file_name}.pkl")
                        logger.verbose("%s.pkl created", file_name)
                    else:
                        logger.warning(
                            "File is very large [%s KB]. \n Recommend enabling pickle from the configuration file to save this file."
                            % (sys.getsizeof(data) / 1024)
                        )
                break

            except PermissionError as pe:
                logger.error(
                    f"Permission denied writing to {file_name}: {pe}", exc_info=True
                )
                raise WizFileError(f"Permission denied writing to {file_name}", pe)

            except (IOError, OSError) as e:
                logger.error(f"I/O error writing {file_name}: {e}", exc_info=True)
                raise WizFileError(f"Failed to write {file_name}", e)

            except Exception as e:
                logger.error(
                    f"Unexpected error writing {file_name}: {e}", exc_info=True
                )
                raise WizFileError(f"Unexpected error writing {file_name}", e)

        duration = return_formatted_duration(time.perf_counter() - start)
        logger.debug(f"{file_name} export completed (Duration: {duration})")
    else:
        logger.debug("No Data to save")


def store_data(data: Any, file_path: Union[str, Path, None] = None) -> Any:
    """Pickle data to disk and return it."""
    import pickle
    import mimetypes

    mimetypes.add_type("application/python-pickle", ".pkl")
    mimetypes.add_type("application/python-pickle", ".pickle")
    if file_path is None:
        file_path = Path.cwd() / "data.pkl"
    try:
        with open(file_path, "wb") as pickle_file:
            pickle.dump(data, pickle_file)
            return data
    except Exception as e:
        logging.error(f"Data not saved due to error: {e}", exc_info=True)


def return_formatted_duration(duration: float) -> str:
    """Convert a duration in seconds to a human-readable string (e.g. '2 hours, 3 minutes')."""
    formatted_duration: Dict[str, Union[str, int]] = {
        "hour": "",
        "minute": "",
        "second": "",
        "millisecond": "",
    }
    hours = duration // 3600
    remaining_duration = duration % 3600

    minutes = remaining_duration // 60
    remaining_duration = remaining_duration % 60

    seconds = remaining_duration // 1
    milliseconds = (remaining_duration - seconds) * 1000

    if hours > 0:
        formatted_duration["hour"] = int(hours)
    if minutes > 0:
        formatted_duration["minute"] = int(minutes)
    if seconds > 0:
        formatted_duration["second"] = int(seconds)
    if milliseconds > 0:
        formatted_duration["millisecond"] = int(milliseconds)

    formatted_parts = []
    for key, value in formatted_duration.items():
        if isinstance(value, int) and value > 0:
            formatted_parts.append(f"{value} {key}{'s' if value > 1 else ''}")

    return ", ".join(formatted_parts)


def extract_fields(selection_set: Any) -> Dict[str, Any]:
    """Recursively extract fields from a GraphQL selection set."""
    fields: Dict[str, Any] = {}
    for selection in selection_set.selections:
        if selection.kind == "field":
            name = selection.name.value
            if selection.selection_set:
                # Recurse into nested fields
                fields[name] = extract_fields(selection.selection_set)
            else:
                fields[name] = None
        elif selection.kind == "fragment_spread":
            # Handle fragment spreads if needed
            fields[selection.name.value] = "FRAGMENT"
        elif selection.kind == "inline_fragment":
            # Handle inline fragments if needed
            if selection.selection_set:
                fields.update(extract_fields(selection.selection_set))
    return fields


def parse_query_metadata(query: str) -> Dict[str, Any]:
    """Parse a GraphQL query string and return its type, name, fields, and source."""
    document = parse(query)
    request_type = ""
    request_name = ""
    fields: Dict[str, Any] = {}

    for definition in document.definitions:
        if definition.kind == "operation_definition":
            assert isinstance(definition, OperationDefinitionNode)
            request_type = str(definition.operation).split(".")[-1]
            request_name = definition.name.value if definition.name else ""
            fields = extract_fields(definition.selection_set)
            break  # Usually just one operation

    return {
        "request_type": request_type,
        "request_name": request_name,
        "fields": fields,
        "source": next(iter(fields)),
    }


def ensure_pagination_variables(query: str) -> str:
    """Inject $after into a query if it uses the Relay connection pattern but is missing the variable.

    Conditions for injection:
    1. The operation is a query (not a mutation)
    2. A top-level field contains both 'nodes' and 'pageInfo' subfields (Relay connection pattern)
    3. '$after' is not already declared in the variable definitions

    Returns the modified query string, or the original if no injection is needed.
    """
    try:
        document = parse(query)
    except Exception:
        return query

    for definition in document.definitions:
        if definition.kind != "operation_definition":
            continue
        assert isinstance(definition, OperationDefinitionNode)
        if str(definition.operation).split(".")[-1].lower() != "query":
            continue

        # Check if $after is already declared
        existing_vars = {
            v.variable.name.value for v in (definition.variable_definitions or [])
        }
        if "after" in existing_vars:
            return query

        # Find the paginated field (has both 'nodes' and 'pageInfo' subfields)
        paginated_field: Optional[FieldNode] = None
        for selection in definition.selection_set.selections:
            if not isinstance(selection, FieldNode) or not selection.selection_set:
                continue
            child_names = {
                s.name.value
                for s in selection.selection_set.selections
                if isinstance(s, FieldNode)
            }
            if "nodes" in child_names and "pageInfo" in child_names:
                paginated_field = selection
                break

        if paginated_field is None:
            return query

        # Check if the field already has an 'after' argument
        existing_args = {a.name.value for a in (paginated_field.arguments or [])}
        if "after" in existing_args:
            return query

        # Inject $after: String into variable definitions
        after_var_def = VariableDefinitionNode(
            variable=VariableNode(name=NameNode(value="after")),
            type=NamedTypeNode(name=NameNode(value="String")),
        )
        new_var_defs = list(definition.variable_definitions or []) + [after_var_def]

        # Inject after: $after into the paginated field's arguments
        after_arg = ArgumentNode(
            name=NameNode(value="after"),
            value=VariableNode(name=NameNode(value="after")),
        )
        new_args = list(paginated_field.arguments or []) + [after_arg]

        # Rebuild the AST with injected variable and argument
        new_doc = copy.deepcopy(document)
        new_defn_node = new_doc.definitions[document.definitions.index(definition)]
        assert isinstance(new_defn_node, OperationDefinitionNode)
        new_defn_node.variable_definitions = tuple(new_var_defs)
        for sel in new_defn_node.selection_set.selections:
            if (
                isinstance(sel, FieldNode)
                and sel.name.value == paginated_field.name.value
            ):
                sel.arguments = tuple(new_args)
                break
        return print_ast(new_doc)

    return query


def has_totalcount_field(query_fields: Dict[str, Any]) -> bool:
    """Return True if the root connection field in parsed query metadata includes totalCount.

    Expects the 'fields' dict from parse_query_metadata() — a single top-level key
    whose value is a dict of the connection's child fields.
    """
    if not query_fields:
        return False
    root_children = next(iter(query_fields.values()), None)
    if not isinstance(root_children, dict):
        return False
    return "totalCount" in root_children


def build_totalcount_probe_query(query: str) -> Optional[str]:
    """AST-transform a query to select only { totalCount } on the root connection field.

    Preserves all variable definitions and field arguments (filterBy, first, etc.) so
    the probe uses identical filter variables to the original query.

    Returns None if the query cannot be transformed (mutation, no connection field,
    connection field has no totalCount child, etc.).
    """
    try:
        document = parse(query)
    except Exception:
        return None

    for definition in document.definitions:
        if definition.kind != "operation_definition":
            continue
        assert isinstance(definition, OperationDefinitionNode)
        if str(definition.operation).split(".")[-1].lower() != "query":
            return None  # mutations and subscriptions are never split

        # Find the root connection field — must have a totalCount child
        connection_field: Optional[FieldNode] = None
        for selection in definition.selection_set.selections:
            if not isinstance(selection, FieldNode) or not selection.selection_set:
                continue
            child_names = {
                s.name.value
                for s in selection.selection_set.selections
                if isinstance(s, FieldNode)
            }
            if "totalCount" in child_names:
                connection_field = selection
                break

        if connection_field is None:
            return None

        # Build a minimal selection set: just totalCount
        totalcount_field = FieldNode(name=NameNode(value="totalCount"))
        from graphql.language.ast import SelectionSetNode

        minimal_selection = SelectionSetNode(selections=(totalcount_field,))

        # Deep-copy and replace the connection field's selection set
        new_doc = copy.deepcopy(document)
        new_defn = new_doc.definitions[document.definitions.index(definition)]
        assert isinstance(new_defn, OperationDefinitionNode)
        for sel in new_defn.selection_set.selections:
            if (
                isinstance(sel, FieldNode)
                and sel.name.value == connection_field.name.value
            ):
                sel.selection_set = minimal_selection
                # Drop $first/$after variable definitions — they're irrelevant for a count query
                new_defn.variable_definitions = tuple(
                    v
                    for v in (new_defn.variable_definitions or [])
                    if v.variable.name.value not in ("first", "after")
                )
                # Drop first/after arguments from the connection field too
                sel.arguments = tuple(
                    a
                    for a in (sel.arguments or [])
                    if a.name.value not in ("first", "after")
                )
                break

        return print_ast(new_doc)

    return None


def inject_subscription_filter(
    vars: Dict[str, Any], filter_path: str, ids: List[str]
) -> Dict[str, Any]:
    """Return a deep copy of vars with filter_path set to ids.

    filter_path uses dot-notation, e.g. 'filterBy.subscriptionId'.
    Intermediate dicts are created as needed; existing sibling keys are preserved.

    Example:
        inject_subscription_filter({'filterBy': {'severity': 'HIGH'}},
                                   'filterBy.subscriptionId', ['abc'])
        -> {'filterBy': {'severity': 'HIGH', 'subscriptionId': ['abc']}}
    """
    result = copy.deepcopy(vars)
    keys = filter_path.split(".")
    d: Any = result
    for key in keys[:-1]:
        if not isinstance(d.get(key), dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = ids
    return result


@disable_in_serverless
def load_credentials_from_file(
    profile: str, credentials_file: str
) -> Dict[str, Optional[str]]:
    """Load client_id, client_secret, and environment from an INI credentials file."""
    logger = Config.get_logger()
    config = configparser.ConfigParser()
    path = Path(credentials_file)
    if not path.exists():
        logger.debug(f"Credentials file {credentials_file} does not exist.")
        return {}
    config.read(path)
    if profile in config:
        logger.debug(f"Loading credentials for profile: {profile}")
        client_id = config[profile].get("client_id")
        client_secret = config[profile].get("client_secret")
        environment = config[profile].get("environment", None)
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "environment": environment,
        }
    logger.debug(f"Profile {profile} not found in credentials file.")
    return {}


@disable_in_serverless
def write_credentials_to_file(
    profile: str,
    client_id: str,
    client_secret: str,
    credentials_file: str,
    environment: Optional[str] = None,
) -> None:
    """Write credentials for a profile to an INI credentials file."""
    config = configparser.ConfigParser()
    path = Path(credentials_file)
    try:
        if path.exists():
            config.read(path)
        if profile not in config:
            config.add_section(profile)
        config[profile]["client_id"] = client_id
        config[profile]["client_secret"] = client_secret
        if environment:
            config[profile]["environment"] = environment
        with open(path, "w") as f:
            config.write(f)
        Config.get_logger().debug(
            f"Credentials for profile {profile} written to {credentials_file}"
        )
    except (IOError, OSError) as e:
        Config.get_logger().error(
            f"Failed to write credentials to {credentials_file}: {e}", exc_info=True
        )
        raise WizFileError(f"Failed to write credentials to {credentials_file}", e)
    except Exception as e:
        Config.get_logger().error(
            f"Unexpected error writing credentials to {credentials_file}: {e}",
            exc_info=True,
        )
        raise WizFileError(
            f"Unexpected error writing credentials to {credentials_file}", e
        )


def is_in_last_x_intervals(
    timestamp_str: Union[str, datetime],
    interval_value: int = 1,
    interval_type: str = "days",
) -> bool:
    """Check whether a timestamp falls within the last N days/minutes/seconds."""
    if interval_type not in ["days", "minutes", "seconds"]:
        raise ValueError("Invalid interval type. Use 'days', 'minutes', or 'seconds'.")

    if interval_value <= 0:
        return True

    time_difference = timedelta(**{interval_type: interval_value})
    current_time = datetime.now(timezone.utc)

    if isinstance(timestamp_str, str):
        time_format = determine_time_format(timestamp_str)
        if time_format is None:
            raise ValueError(f"Unable to determine time format for: {timestamp_str}")
        given_time = datetime.strptime(timestamp_str, time_format)
        if given_time.tzinfo is None:
            given_time = given_time.replace(tzinfo=timezone.utc)
    else:
        given_time = timestamp_str
        if given_time.tzinfo is None:
            given_time = given_time.replace(tzinfo=timezone.utc)

    return given_time >= (current_time - time_difference).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def determine_time_format(date_str: str) -> Optional[str]:
    """Return the matching strptime format string for the given date, or None."""
    possible_formats = [
        # ISO 8601 with microseconds
        "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO 8601 with microseconds and timezone offset
        "%Y-%m-%dT%H:%M:%S.%f",  # ISO 8601 with microseconds, no timezone or offset
        "%Y-%m-%dT%H:%M:%S.%fZ",  # ISO 8601 with microseconds and 'Z' for UTC
        # ISO 8601 without microseconds
        "%Y-%m-%dT%H:%M:%S%z",  # ISO 8601 without microseconds, timezone offset
        "%Y-%m-%dT%H:%M:%SZ",  # ISO 8601 without microseconds, 'Z' for UTC
        "%Y-%m-%d %H:%M:%S.%f%z",  # ISO 8601 with space instead of 'T', microseconds and timezone
        "%Y-%m-%d %H:%M:%S.%f",  # ISO 8601 with space instead of 'T', microseconds, no timezone
        "%Y-%m-%d %H:%M:%S%z",  # ISO 8601 with space instead of 'T', no microseconds, timezone offset
        "%Y-%m-%d %H:%M:%SZ",  # ISO 8601 with space instead of 'T', no microseconds, 'Z' for UTC
        # ISO 8601 Week-based date format
        "%G-W%V-%u",  # ISO 8601 week-based date format (e.g., "2024-W31-3")
        # RFC 822/2822 formats
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 822 / RFC 2822 format (e.g., "Tue, 31 Jul 2024 16:14:51 +0000")
        # Full date and time with various precision
        "%Y-%m-%d %H:%M:%S.%f %z %Z",  # Full date and time with microseconds, timezone offset, and abbreviation (UTC)
        "%Y-%m-%d %H:%M:%S.%f",  # Full date and time with microseconds, no timezone or offset
        "%Y-%m-%d %H:%M:%S",  # Full date and time without microseconds or timezone
        # Compact date and time formats
        "%Y%m%d%H%M%S",  # Compact date and time format (e.g., "20240731161451")
        "%Y%m%d",  # Compact date format (e.g., "20240731")
        # Date only formats
        "%Y-%m-%d",  # Date only in YYYY-MM-DD format
        "%d/%m/%Y",  # Date only in DD/MM/YYYY format (European style)
        "%m/%d/%Y",  # Date only in MM/DD/YYYY format (US style)
        "%Y/%m/%d",  # Date only in YYYY/MM/DD format
        # Date and time with different regions
        "%d/%m/%Y %H:%M:%S",  # Date and time in DD/MM/YYYY HH:MM:SS format (European style)
        "%m/%d/%Y %H:%M:%S",  # Date and time in MM/DD/YYYY HH:MM:SS format (US style)
        "%Y/%m/%d %H:%M:%S",  # Date and time in YYYY/MM/DD HH:MM:SS format
        "%Y-%m-%d %I:%M %p",  # Date and time in YYYY-MM-DD with 12-hour time and AM/PM
        # Time-only formats
        "%H:%M:%S",  # Time only in HH:MM:SS format (24-hour clock)
        "%I:%M %p",  # Time only in HH:MM AM/PM format (12-hour clock)
        "%H:%M:%S.%f",  # Time only with microseconds (HH:MM:SS.microseconds)
        # Date and time with full day and month names
        "%d %b %Y",  # Date with day, abbreviated month, and year (e.g., "31 Jul 2024")
        "%A, %d %B %Y %H:%M:%S",  # Full day name, date, full month name, year, and time (e.g., "Wednesday, 31 July 2024 16:14:51")
    ]

    for fmt in possible_formats:
        try:
            datetime.strptime(date_str, fmt)
            return fmt
        except ValueError:
            continue

    return None


def load_file_if_in_last_x_interval(
    filepath: str,
    interval_value: Optional[int] = None,
    interval_type: str = "days",
    extension_order: List[str] = [".pkl", ".json", ".csv"],
    backend: Optional["CacheBackend"] = None,
) -> Any:
    """Load cached data only if it was stored within the given interval.

    When a remote backend (S3, DynamoDB) is provided or configured, the
    filepath is used as the cache key rather than a local path.  Pass
    backend=False to force filesystem behaviour regardless of config.
    """
    resolved_backend: Optional["CacheBackend"] = None

    if backend is False:
        # Caller explicitly wants filesystem — fall through to legacy path
        pass
    elif backend is not None:
        resolved_backend = backend
    else:
        if Config.get("cache", "allow_cache", default=False):
            resolved_backend = get_configured_backend()

    if resolved_backend is not None:
        if interval_value is not None and interval_value < 0:
            return {}

        data, ts = resolved_backend.get(filepath)
        if data is None:
            return {}

        if interval_value is None:
            return data

        if is_in_last_x_intervals(
            ts, interval_value=interval_value, interval_type=interval_type
        ):
            return data
        return {}

    # --- Filesystem path (original behaviour) ---
    if interval_value:
        if interval_value < 0:
            return {}
    else:
        full_path = find_file_with_extension(filepath, extension_order)
        return load_file(full_path)

    full_path = find_file_with_extension(filepath, extension_order)

    if not full_path or os.path.getsize(full_path) <= 1000:
        return {}

    last_modified_time = datetime.fromtimestamp(os.path.getmtime(full_path))

    if is_in_last_x_intervals(
        last_modified_time, interval_value=interval_value, interval_type=interval_type
    ):
        return load_file(full_path)

    return {}


def load_file(full_path: str) -> Any:
    """Load a file based on its MIME type."""
    mime_type, _ = mimetypes.guess_type(full_path)

    if mime_type == "application/json":
        return load_json(full_path)
    elif mime_type == "text/csv":
        return load_csv(full_path)
    elif mime_type in ["application/xml", "text/xml"]:
        return load_xml(full_path)
    elif mime_type == "text/plain":
        return load_text(full_path)
    elif mime_type == "application/python-pickle":
        return load_data(full_path)
    else:
        return {}
        # raise ValueError(f"Unsupported file type for {full_path}")


def load_json(full_path: str) -> Any:
    """Load and normalize a JSON file."""
    with open(full_path, "r") as f:
        file_data = json.load(f)

        if (
            isinstance(file_data, dict)
            and len(file_data) == 1
            and isinstance(next(iter(file_data.values())), list)
        ):
            data = next(iter(file_data.values()))
            unique_keys = {key for item in data for key in item.keys()}
            for item in data:
                for key in unique_keys:
                    if key not in item:
                        item[key] = None
            return {next(iter(file_data)): data}

        return file_data


def load_csv(full_path: str) -> List[Dict[str, str]]:
    """Load a CSV file into a list of dictionaries."""
    with open(full_path, "r") as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_xml(full_path: str) -> ET.Element:
    """Load an XML file and return the root element."""
    tree = ET.parse(full_path)
    return tree.getroot()


def load_text(full_path: str) -> str:
    """Load a plain text file and return its contents."""
    with open(full_path, "r") as f:
        return f.read()


def load_data(file_path: str) -> Any:
    """Load a pickled data file from disk."""
    try:
        with open(file_path, "rb") as pickled_data:
            dataFile = pickle.load(pickled_data)
            logging.info(
                f"Data dated {datetime.fromtimestamp(os.path.getmtime(file_path))} loaded."
            )
        return dataFile
    except (IOError, OSError, pickle.PickleError) as e1:
        message = f"Unable to open saved data due to error: {e1}"
        logging.error(message, exc_info=True)
        raise WizFileError(f"Failed to load data from {file_path}", e1)
