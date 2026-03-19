"""
Report generation example.

Demonstrates:
- Creating a report via mutation
- Automatic polling until the report is ready
- Streaming vs full download modes
- Page-level progress tracking for streamed reports
"""

from wizsec import WizClient, Config

Config.load()
client = WizClient()


def on_progress(event):
    """Called for each chunk of a streamed report."""
    name = event["name"]
    downloaded = event.get("downloaded", 0)
    total = event.get("total_size", 0)
    if total:
        pct = (downloaded / total) * 100
        print(f"  [{name}] {pct:.1f}% ({downloaded}/{total} bytes)")
    else:
        print(f"  [{name}] {downloaded} bytes downloaded")


# --- Streamed report (default) ---
# The SDK will:
#   1. Execute the createReport mutation
#   2. Poll until status == COMPLETED
#   3. Stream the download URL line-by-line
#   4. Attach parsed rows to result.data["report_data"]

print("=== Streaming Report ===")
response = client.create_request(
    query="""
        mutation CreateVulnReport {
            createReport(input: {
                name: "Vulnerability Summary"
                type: VULNERABILITIES
            }) {
                report { id }
            }
        }
    """,
    report_request={
        "name": "Vulnerability Summary",
        "stream": True,
    },
    on_page_event=on_progress,
)
result = response.submit()

if result.success:
    rows = result.data.get("report_data", [])
    print(f"Received {len(rows)} rows")
else:
    print(f"Report failed: {result.errors}")


# --- Full download (non-streamed) ---
# Set stream=False to download the entire report at once as raw bytes.

print("\n=== Full Download Report ===")
response = client.create_request(
    query="""
        mutation CreateAssetReport {
            createReport(input: {
                name: "Asset Inventory"
                type: HOST_CONFIGURATION
            }) {
                report { id }
            }
        }
    """,
    report_request={
        "name": "Asset Inventory",
        "stream": False,
    },
)
result = response.submit()

if result.success:
    raw = result.data.get("report_data")
    print(f"Downloaded {len(raw) if raw else 0} bytes")
else:
    print(f"Report failed: {result.errors}")
