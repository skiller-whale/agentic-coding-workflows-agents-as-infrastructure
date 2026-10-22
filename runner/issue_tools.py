"""Run-scoped issue tools, exposed to Claude over MCP stdio."""

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP


MCP_TOOLS = {name: f"mcp__whalehub__{name}" for name in
             ("SetIssueLabels", "AddIssueComment", "CreateIssue", "ReadIssue", "ReadIssues")}


def validate_labels(labels):
    if (not isinstance(labels, list)
            or any(not isinstance(label, str) or not label.strip() for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError("labels must be a list of distinct non-empty strings; [] clears the labels.")
    return labels


def create_server(directory):
    job = json.loads((directory / "claimed.json").read_text(encoding="utf-8"))
    server = FastMCP("whalehub", log_level="WARNING")

    def call(action, payload=None):
        base_url = os.environ.get("WHALEHUB_API_URL", "http://whalehub:8000").rstrip("/")
        request = Request(f"{base_url}/api/runs/{job['run_id']}/{action}",
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as error:
            detail = json.loads(error.read()).get("error", str(error))
            raise ValueError("Issue tool failed: " + detail) from error
        except URLError as error:
            raise ValueError("Could not reach Whalehub: " + str(error.reason)) from error

    if "SetIssueLabels" in job["workflow"]["tools"]:
        @server.tool(name="SetIssueLabels")
        def set_issue_labels(issue_number: int, labels: list[str]) -> str:
            """Immediately replace the given issue's labels. Use [] to clear them."""
            validate_labels(labels)
            updated = call("labels", {"issue_number": issue_number, "labels": labels})
            return f"Updated labels on issue #{updated['issue_number']}: {json.dumps(updated['labels'])}."

    if "AddIssueComment" in job["workflow"]["tools"]:
        @server.tool(name="AddIssueComment")
        def add_issue_comment(issue_number: int, body: str) -> str:
            """Post a comment on the given issue immediately. Supply the comment text as body."""
            updated = call("comments", {"issue_number": issue_number, "body": body})
            return f"Posted comment #{updated['comment']['id']} on issue #{updated['issue_number']}."

    if "CreateIssue" in job["workflow"]["tools"]:
        @server.tool(name="CreateIssue")
        def create_issue(title: str, body: str) -> dict:
            """Create an issue and return its number, title, body, labels and comments."""
            return call("issues", {"title": title, "body": body})

    if "ReadIssue" in job["workflow"]["tools"]:
        @server.tool(name="ReadIssue")
        def read_issue(issue_number: int) -> dict:
            """Read one issue's current title, body, labels and comments by its number."""
            return call(f"issues/{issue_number}")

    if "ReadIssues" in job["workflow"]["tools"]:
        @server.tool(name="ReadIssues")
        def read_issues() -> dict:
            """Read all current issues, including their numbers, descriptions, labels and comments."""
            return call("issues")

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    create_server(args.run_directory).run(transport="stdio")
