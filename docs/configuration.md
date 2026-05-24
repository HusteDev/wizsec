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
#   export_directory: ""            # default: working directory
#   export_type: json               # json | csv
#   retry_time: 30
#   max_retries: 3
#   polling_time: 15

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
