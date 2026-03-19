# Getting Started

## Prerequisites

- Python 3.9+
- A Wiz account with API credentials (client ID + secret)

## Installation

```bash
pip install wizsec
```

For development:

```bash
pip install -e ".[dev]"
```

## Credentials Setup

### Option 1: Credentials File (default)

Create `~/.wiz/wiz.credentials` in INI format:

```ini
[default]
client_id = your-client-id
client_secret = your-client-secret
environment = gov
```

Multiple profiles are supported:

```ini
[default]
client_id = prod-client-id
client_secret = prod-client-secret
environment = gov

[staging]
client_id = staging-client-id
client_secret = staging-client-secret
environment = app
```

### Option 2: Environment Variables

```bash
export WIZ_CLIENT_ID=your-client-id
export WIZ_CLIENT_SECRET=your-client-secret
```

### Option 3: Constructor Arguments

```python
client = WizClient(
    client_id="your-client-id",
    client_secret="your-client-secret",
    environment="gov",
)
```

## First Query

```python
from wizsec import WizClient, Config

# Load config from ~/.wiz/wiz.config (auto-generated if missing)
Config.load()

# Create a client using the "default" profile
client = WizClient()

# Run a query
response = client.create_request(
    query="""
        query ListProjects($first: Int) {
            projects(first: $first) {
                nodes { id name }
                pageInfo { hasNextPage endCursor }
            }
        }
    """,
    vars={"first": 50},
)
result = response.submit()

if result.success():
    for project in result.data["projects"]["nodes"]:
        print(project["name"])
else:
    for error in result.errors:
        print(error["message"])
```

## Environments

Wiz has three environments:

| Environment | Domain             | Use Case                |
| ----------- | ------------------ | ----------------------- |
| `app`       | `app.wiz.io`       | Commercial cloud        |
| `gov`       | `gov.wiz.io`       | Government cloud        |
| `fedramp`   | `app.wiz.us`       | FedRAMP                 |

Set the environment in your credentials file, config, or constructor:

```python
client = WizClient(environment="gov")
```
