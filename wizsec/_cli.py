##
##
##    CCCCC   L       IIIIIII
##   C        L          I
##   C        L          I
##   C        L          I
##    CCCCC   LLLLLL  IIIIIII
##
##
##

# _cli.py
"""Command-line interface for managing wizsec configuration and credentials.

Installed as the ``wizsec`` console script. All commands operate on the same
files the SDK uses at runtime: the YAML config (default ``~/.wiz/wiz.config``)
and the INI credentials file (default ``~/.wiz/wiz.credentials``).
"""

import argparse
import configparser
import getpass
import sys
from pathlib import Path
from typing import Any, List, Optional

import yaml

from .config import DEFAULT_WIZ_DIR, generate_default_config
from .version import __version__

_CONFIG_FILE = DEFAULT_WIZ_DIR / "wiz.config"
_CREDENTIALS_FILE = DEFAULT_WIZ_DIR / "wiz.credentials"


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _load_yaml_file(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * 8


# ---------------------------------------------------------------------------
# config subcommands
# ---------------------------------------------------------------------------


def _cmd_config_init(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if path.exists() and not args.force:
        return _fail(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    generate_default_config(path)
    return 0


def _cmd_config_path(args: argparse.Namespace) -> int:
    print(args.file)
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        return _fail(f"no config file at {path} (run 'wizsec config init')")
    if args.raw:
        print(path.read_text(encoding="utf-8"))
        return 0
    data = _load_yaml_file(path)
    print(yaml.safe_dump(data or {}, default_flow_style=False, sort_keys=False))
    return 0


def _cmd_config_get(args: argparse.Namespace) -> int:
    data = _load_yaml_file(Path(args.file))
    if data is None:
        return _fail(f"no config file at {args.file}")
    node: Any = data
    for part in args.key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _fail(f"key not found: {args.key}")
        node = node[part]
    if isinstance(node, (dict, list)):
        print(yaml.safe_dump(node, default_flow_style=False, sort_keys=False).rstrip())
    else:
        print(node)
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    path = Path(args.file)
    data = _load_yaml_file(path)
    if data is None:
        return _fail(f"no config file at {path} (run 'wizsec config init')")

    value = yaml.safe_load(args.value)
    node = data
    parts = args.key.split(".")
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value

    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    print(f"set {args.key} = {value!r}")
    print("note: rewriting the file drops YAML comments; see the bundled")
    print("template for documentation of every option.")
    return 0


def _cmd_config_unset(args: argparse.Namespace) -> int:
    path = Path(args.file)
    data = _load_yaml_file(path)
    if data is None:
        return _fail(f"no config file at {path}")
    node = data
    parts = args.key.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return _fail(f"key not found: {args.key}")
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return _fail(f"key not found: {args.key}")
    del node[parts[-1]]
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    print(f"unset {args.key}")
    return 0


# ---------------------------------------------------------------------------
# creds subcommands
# ---------------------------------------------------------------------------


def _read_credentials(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    return parser


def _cmd_creds_set(args: argparse.Namespace) -> int:
    path = Path(args.file)
    client_id = args.client_id or input(f"[{args.profile}] Client ID: ").strip()
    if not client_id:
        return _fail("client ID must not be empty")
    client_secret = getpass.getpass(f"[{args.profile}] Client Secret: ").strip()
    if not client_secret:
        return _fail("client secret must not be empty")

    parser = _read_credentials(path)
    if args.profile not in parser:
        parser.add_section(args.profile)
    parser[args.profile]["client_id"] = client_id
    parser[args.profile]["client_secret"] = client_secret
    if args.environment:
        parser[args.profile]["environment"] = args.environment

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)
    print(f"credentials saved for profile '{args.profile}' in {path}")
    return 0


def _cmd_creds_list(args: argparse.Namespace) -> int:
    path = Path(args.file)
    parser = _read_credentials(path)
    profiles = parser.sections()
    if not profiles:
        print(f"no profiles in {path}")
        return 0
    for profile in profiles:
        client_id = parser[profile].get("client_id", "")
        environment = parser[profile].get("environment", "-")
        print(f"{profile}: client_id={_mask(client_id)} environment={environment}")
    return 0


def _cmd_creds_remove(args: argparse.Namespace) -> int:
    path = Path(args.file)
    parser = _read_credentials(path)
    if args.profile not in parser:
        return _fail(f"profile '{args.profile}' not found in {path}")
    if not args.yes:
        answer = input(f"remove credentials for profile '{args.profile}'? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    parser.remove_section(args.profile)
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)
    print(f"removed profile '{args.profile}'")
    return 0


def _cmd_creds_test(args: argparse.Namespace) -> int:
    from .client import WizClient
    from .exceptions import WizError

    try:
        client = WizClient(
            environment=args.environment or "",
            profile=args.profile,
        )
        client._check_token()
    except WizError as exc:
        return _fail(f"authentication failed: {exc}")
    print(
        f"authentication OK — environment={client.environment} "
        f"dc={client.dc} service_account={client.is_service_account}"
    )
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the local wizsec setup without side effects."""
    import time as _time

    from ._registry import PUBLISHED_RATE_LIMITS

    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        if level == "FAIL":
            failures += 1
        print(f"[{level:>4}] {message}")

    # -- config file
    config_path = Path(args.config_file)
    config_data: Any = None
    if not config_path.exists():
        report("FAIL", f"config file missing: {config_path} (run 'wizsec config init')")
    else:
        try:
            config_data = _load_yaml_file(config_path) or {}
            schema_ver = (config_data.get("app") or {}).get("config_schema")
            report("OK", f"config file parsed: {config_path} (schema v{schema_ver})")
        except yaml.YAMLError as exc:
            report("FAIL", f"config file is not valid YAML: {exc}")

    # -- credentials file
    creds_path = Path(args.creds_file)
    profiles: List[str] = []
    if not creds_path.exists():
        report(
            "WARN", f"credentials file missing: {creds_path} (run 'wizsec creds set')"
        )
    else:
        profiles = _read_credentials(creds_path).sections()
        if profiles:
            report(
                "OK",
                f"credentials file has {len(profiles)} profile(s): {', '.join(profiles)}",
            )
        else:
            report("WARN", f"credentials file has no profiles: {creds_path}")

    # -- effective rate budgets (defaults if config missing)
    rate_cfg = (config_data or {}).get("rate_limit") or {}
    try:
        headroom = float(rate_cfg.get("headroom", 0.8))
        if headroom <= 0:
            headroom = 0.8
    except (TypeError, ValueError):
        headroom = 0.8
    overrides = rate_cfg.get("overrides") or {}
    budgets = []
    for key, published in PUBLISHED_RATE_LIMITS.items():
        try:
            effective = float(overrides.get(key, published * headroom))
        except (TypeError, ValueError):
            effective = published * headroom
        budgets.append(f"{key}={effective:g}/s")
    report("OK", f"rate budgets (headroom {headroom:g}): {', '.join(budgets)}")

    # -- schema caches
    wiz_dir = config_path.parent
    schema_files = sorted(wiz_dir.glob("schema_*.json"))
    if not schema_files:
        report(
            "WARN",
            f"no schema caches in {wiz_dir} (query validation will introspect on first use)",
        )
    else:
        for f in schema_files:
            age_days = (_time.time() - f.stat().st_mtime) / 86400
            level = "WARN" if age_days > 30 else "OK"
            report(level, f"schema cache {f.name}: {age_days:.0f} day(s) old")

    # -- optional live authentication check
    if args.auth:
        auth_args = argparse.Namespace(
            profile=args.profile, environment=args.environment
        )
        if _cmd_creds_test(auth_args) != 0:
            failures += 1
    else:
        report("OK", "auth check skipped (pass --auth to authenticate)")

    print()
    if failures:
        print(f"doctor found {failures} problem(s)")
        return 1
    print("doctor found no problems")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wizsec",
        description="Manage wizsec SDK configuration and credentials.",
    )
    parser.add_argument("--version", action="version", version=f"wizsec {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # -- config
    config_parser = subparsers.add_parser("config", help="manage the YAML config file")
    config_parser.add_argument(
        "--file",
        default=str(_CONFIG_FILE),
        help=f"config file path (default: {_CONFIG_FILE})",
    )
    config_sub = config_parser.add_subparsers(dest="config_command")

    p = config_sub.add_parser("init", help="create a default config file")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=_cmd_config_init)

    p = config_sub.add_parser("path", help="print the config file path")
    p.set_defaults(func=_cmd_config_path)

    p = config_sub.add_parser("show", help="print the effective config")
    p.add_argument("--raw", action="store_true", help="print the file verbatim")
    p.set_defaults(func=_cmd_config_show)

    p = config_sub.add_parser("get", help="read a value by dotted key")
    p.add_argument("key", help="dotted key, e.g. rate_limit.headroom")
    p.set_defaults(func=_cmd_config_get)

    p = config_sub.add_parser("set", help="write a value by dotted key")
    p.add_argument("key", help="dotted key, e.g. rate_limit.headroom")
    p.add_argument("value", help="value (YAML-typed: 0.8, true, text)")
    p.set_defaults(func=_cmd_config_set)

    p = config_sub.add_parser("unset", help="remove a key")
    p.add_argument("key", help="dotted key to remove")
    p.set_defaults(func=_cmd_config_unset)

    # -- creds
    creds_parser = subparsers.add_parser("creds", help="manage stored credentials")
    creds_parser.add_argument(
        "--file",
        default=str(_CREDENTIALS_FILE),
        help=f"credentials file path (default: {_CREDENTIALS_FILE})",
    )
    creds_sub = creds_parser.add_subparsers(dest="creds_command")

    p = creds_sub.add_parser("set", help="store credentials for a profile")
    p.add_argument("--profile", default="default")
    p.add_argument("--environment", default="", help="app, gov, or fedramp")
    p.add_argument("--client-id", default="", help="client ID (prompted if omitted)")
    p.set_defaults(func=_cmd_creds_set)

    p = creds_sub.add_parser("list", help="list stored profiles (secrets never shown)")
    p.set_defaults(func=_cmd_creds_list)

    p = creds_sub.add_parser("remove", help="remove a profile")
    p.add_argument("profile")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=_cmd_creds_remove)

    p = creds_sub.add_parser("test", help="authenticate with stored credentials")
    p.add_argument("--profile", default="default")
    p.add_argument("--environment", default="", help="app, gov, or fedramp")
    p.set_defaults(func=_cmd_creds_test)

    # -- doctor
    p = subparsers.add_parser(
        "doctor", help="diagnose config, credentials, and rate-limit setup"
    )
    p.add_argument(
        "--config-file",
        default=str(_CONFIG_FILE),
        help=f"config file path (default: {_CONFIG_FILE})",
    )
    p.add_argument(
        "--creds-file",
        default=str(_CREDENTIALS_FILE),
        help=f"credentials file path (default: {_CREDENTIALS_FILE})",
    )
    p.add_argument(
        "--auth", action="store_true", help="also authenticate against the API"
    )
    p.add_argument("--profile", default="default")
    p.add_argument("--environment", default="", help="app, gov, or fedramp")
    p.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
