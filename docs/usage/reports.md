# Reports

The SDK can create Wiz reports, poll for completion, and stream or download results.

## Creating a Report

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient()

response = client.create_request(
    query="""
        mutation CreateReport($input: CreateReportInput!) {
            createReport(input: $input) {
                report { id name }
            }
        }
    """,
    vars={
        "input": {
            "name": "My Report",
            "type": "DETAILED",
            "projectId": "project-id-here",
        }
    },
    report_request={
        "name": "My Report",
        "stream": True,  # stream results as they arrive
    },
)
result = response.submit()
```

When the query is a `createReport` or `rerunReport` mutation, the SDK automatically:

1. Submits the mutation
2. Polls the report status until completion
3. Downloads or streams the report data

## Streaming vs Download

**Streaming** (default) processes results as they arrive — useful for large reports:

```python
report_request={"name": "My Report", "stream": True}
```

**Download** fetches the entire report at once:

```python
report_request={"name": "My Report", "stream": False}
```

The default behavior is controlled by `reports.stream_by_default` in your config.

## Page Events for Reports

Track download progress:

```python
def on_progress(event):
    print(f"Downloaded {event['downloaded']}/{event['total_size']} bytes")

response = client.create_request(
    query="...",
    report_request={"name": "My Report"},
    on_page_event=on_progress,
)
```
