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

## Reports are synchronous only

`report_request` is rejected by `create_async_request()` with a `WizConfigurationError`. Reports are not queries — each one asks the Wiz backend to generate and materialise a dataset, so fanning several out concurrently is far heavier on the API than concurrent queries, and each poll loop would occupy the event loop for minutes. Use the synchronous `client.create_request(...)` for report workflows.

## Bounding a run

`reports.max_retries` caps only *failed* status polls. The overall deadline is `reports.timeout` (default `3600` seconds); when a run exceeds it, polling stops and `response.error` is set to a `WizTimeoutError` naming the last status seen.

Any other way a run can end without a report sets `response.error` to a `WizReportError` carrying `report_id`, `report_name` and `status`: a failure status such as `FAILED` or `EXPIRED`, a `COMPLETED` run with no download URL, or status polling that still fails after `reports.max_retries` attempts (`original_error` then holds the last poll's error).

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
