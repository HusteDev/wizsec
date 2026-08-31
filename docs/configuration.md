# Configuration

The SDK uses a YAML config file at `~/.wiz/wiz.config`. If it doesn't exist, one is auto-generated on first run.

## Config File Reference

```yaml
app:
  name: wizsec
  release: 1.1.0
  config_schema: 2
  # serverless: false           # Lambda/serverless mode
  # wiz_dir: ""                 # Override ~/.wiz directory
  # env_path: ""                # Custom .env file directory

# auth:
#   grant_type: client_credentials  # or device_code (requires WizCode license)
#   credentials:
#     storage_method: file          # file | env | prompt
#     file_path: ""                 # default: ~/.wiz/
#   proxy:                          # blank URLs use environment proxies
#     http:
#       url: ""
#       port: 80
#     https:
#       url: ""
#       port: 80
#   ca_cert: ""                     # path to custom CA bundle (.pem)

# domain:
#   default: gov                    # app | gov | fedramp
#   app:
#     enabled: false
#   gov:
#     enabled: true
#   fedramp:
#     enabled: false

# api:
#   max_retries: 5                  # retry attempts before failure
#   retry_time: 1                   # initial retry wait (seconds)
#   timeout: 120                    # request timeout (seconds)
#   auto_paginate: true             # automatic cursor-based pagination
#   validate_queries: false         # validate against cached schema

# reports:
#   stream_by_default: true
#   retry_time: 30                  # wait between retries after a failed status poll
#   max_retries: 3                  # consecutive failed status polls before giving up
#   polling_time: 15                # wait between status checks while a run is in progress
#   timeout: 3600                   # overall deadline for a run to complete; max_retries
#                                   # only caps failed polls, so without this a run stuck
#                                   # in a healthy status would be polled forever

# rate_limit:
#   headroom: 0.8                   # fraction of Wiz's published limits to use locally
#   overrides:                      # absolute requests/second per limiter key (wins over headroom)
#     query_service: 8
#   max_backoff_waits: 10           # consecutive server rate-limit waits before giving up
#   default_retry_after: 10         # backoff seconds when no usable Retry-After is provided

# logging:
#   enabled: false
#   verbose: false                  # VERBOSE level (15) messages
#   debug: false
#   lowest_level: 10
#   file_handler:
#     enabled: false
#     logging_level: DEBUG
#     log_directory: ""             # default: ~/.wiz/logs/
#     markdown: false               # markdown table format
#   console_handler:
#     enabled: true
#     logging_level: INFO
```

## Invalid Values

Numeric settings are coerced when they are read, so a quoted `timeout: "3600"` works the same as `timeout: 3600`.

A value that cannot be read as a number — or one outside the sensible range — falls back to that setting's default rather than raising. Negative values are always rejected. Zero is accepted where it means something (`max_retries: 0` disables retries, `retry_time: 0` retries immediately) and rejected where it does not (`polling_time: 0` would spin against the API; `timeout: 0` would expire before the first attempt).

Durations are seconds and may be fractional — `retry_time: 0.25` is valid. Counts (`max_retries`, `lowest_level`, the `query_splitting` limits) are whole numbers.

Boolean settings accept the quoted forms too. YAML resolves bare `true`/`false`/`yes`/`no`/`on`/`off` to booleans itself, but a *quoted* `enabled: "false"` arrives as a plain string, and every non-empty string is truthy — so it used to read as `true` and enable what the line was written to disable. Quoted `true/yes/on/y/t/1` and `false/no/off/n/f/0` are now honoured (case- and whitespace-insensitive), and `1`/`0` work as well.

A value that matches none of those — `enabled: "maybe"` — falls back to the setting's default rather than being judged on Python truthiness, so a typo cannot flip a flag on.

The fallback is silent, so a typo in a setting shows up as default behavior rather than an error. `wizsec config show` prints the effective config if a setting does not appear to be taking effect.

## Runtime Configuration

Existing v1 config files migrate to schema v2 automatically when `Config.load()` runs. Existing values are preserved.

### Changing Log Level

```python
from wizsec import Config

Config.load()
Config.set_log_level("DEBUG")

# Or with separate handler level
Config.set_log_level("DEBUG", handler_level="INFO")
```

### Accessing Config Values

```python
Config.get("api", "timeout", default=120)
Config.get("domain", "default", default="gov")
```

`Config.get()` preserves explicit falsy values such as `false`, `0`, and `""`; defaults are used only when a key is missing.
