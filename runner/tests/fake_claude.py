#!/usr/bin/env python3
"""Deterministic subprocess fixture. Never imports or calls Claude."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

def emit(value):
    print(json.dumps(value), flush=True)


mode = os.environ.get("FAKE_MODE", "triage")
with Path(os.environ["FAKE_CALLS"]).open("a") as calls:
    calls.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                           "environment": {key: os.environ.get(key) for key in (
                               "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                               "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_MODEL", "GIT_CONFIG_COUNT")}}) + "\n")
emit({"type": "system", "subtype": "init", "model": "resolved-test-model"})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "I am inspecting the supplied issue."}}})
emit({"type": "stream_event", "event": {"type": "content_block_stop"}})
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "I am inspecting the supplied issue."}]}})
print("Fixture diagnostic", file=sys.stderr, flush=True)


async def issue_tool(name, arguments):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
    server = config["mcpServers"].get("whalehub")
    if not server:
        return
    job = json.loads((Path(server["args"][-1]) / "claimed.json").read_text())
    expected = set(job["workflow"]["tools"]) & {"SetIssueLabels", "AddIssueComment", "CreateIssue", "ReadIssue", "ReadIssues"}
    if name not in expected:
        return
    async with stdio_client(StdioServerParameters(command=server["command"], args=server["args"], env=server.get("env"))) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == expected
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__whalehub__" + name, "input": arguments}]}})
            result = await client.call_tool(name, arguments)
            emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": result.isError,
                 "content": [{"type": "text", "text": block.text} for block in result.content]}]}})
            return result


issue_number = 1

if mode == "single_issue":
    result = asyncio.run(issue_tool("ReadIssue", {"issue_number": 1}))
    assert result and not result.isError
    issue = result.structuredContent or json.loads(next(block.text for block in result.content if hasattr(block, "text")))
    if "result" in issue:
        issue = issue["result"]
    assert issue["number"] == 1 and issue["title"] == "Updated single issue"
    assert issue["comments"][0]["body"] == "Current discussion."

if mode in {"triage", "invalid_labels", "labels_twice", "labels_then_fail", "empty_summary"}:
    labels = ["maintenance", "agent-ready"]
    if mode == "invalid_labels":
        labels = ["duplicate", "duplicate"]
    asyncio.run(issue_tool("SetIssueLabels", {"issue_number": issue_number, "labels": labels}))
    asyncio.run(issue_tool("AddIssueComment", {"issue_number": issue_number, "body": "Triage report: " + ", ".join(labels) + "."}))
    if mode == "labels_twice":
        asyncio.run(issue_tool("SetIssueLabels", {"issue_number": issue_number, "labels": ["custom-label"]}))

if mode == "comments":
    asyncio.run(issue_tool("AddIssueComment", {"issue_number": issue_number, "body": "Reviewed the issue."}))
    asyncio.run(issue_tool("AddIssueComment", {"issue_number": issue_number, "body": " "}))

if mode == "issue_admin":
    async def manage_issues():
        before = await issue_tool("ReadIssues", {})
        assert before and not before.isError
        created = await issue_tool("CreateIssue", {"title": "New agent task", "body": "Created during a workflow without an issue."})
        assert created and not created.isError
        value = created.structuredContent or json.loads(next(block.text for block in created.content if hasattr(block, "text")))
        if "result" in value:
            value = value["result"]
        number = value["number"]
        await issue_tool("SetIssueLabels", {"issue_number": number, "labels": ["new-task"]})
        await issue_tool("AddIssueComment", {"issue_number": number, "body": "Created and labelled by the agent."})
        after = await issue_tool("ReadIssues", {})
        assert after and not after.isError
    asyncio.run(manage_issues())

coding_modes = {"cleanup", "agent_error", "missing_result", "bad_tests", "timeout", "shutdown",
                "uncommitted", "partially_committed", "commit_without_tests"}
if mode in coding_modes:
    source = Path("src/example.py")
    source.write_text(source.read_text().replace("    unused = None\n", ""))
    if mode == "bad_tests":
        source.write_text(source.read_text() + "\nraise RuntimeError('injected fixture failure')\n")
if mode in {"timeout", "shutdown"}:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
    time.sleep(120)


def command(arguments):
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": shlex.join(arguments)}}]}})
    result = subprocess.run(arguments, text=True, capture_output=True)
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": result.returncode != 0,
         "content": [{"type": "text", "text": result.stdout + result.stderr}]}]}})
    return result.returncode


if mode == "alternate_issue":
    source = Path("README.md")
    source.write_text(source.read_text() + "\nSee the examples for CLI usage.\n")
if mode == "untracked":
    Path("src/new_feature.py").write_text('"""Unfinished new feature."""\n')
if mode in coding_modes | {"alternate_issue", "no_changes", "untracked"}:
    test_exit = 0 if mode == "commit_without_tests" else command([sys.executable, "-m", "pytest", "tests", "-q"])
    if test_exit == 0 and mode not in {"no_changes", "uncommitted", "untracked"}:
        assert command(["git", "add", "--all"]) == 0
        assert command(["git", "commit", "-m", "Implement the supplied issue [fixture agent]"]) == 0
    if mode == "partially_committed":
        source.write_text(source.read_text() + "\n# Unfinished follow-up edit.\n")
if mode == "missing_result":
    sys.exit(0)
if mode == "cleanup":
    asyncio.run(issue_tool("AddIssueComment", {"issue_number": issue_number, "body": "Implemented the cleanup; fixture tests passed."}))
result = "A small, clear cleanup." if mode in {"triage", "labels_then_fail"} else "Removed an unused variable."
if mode == "empty_summary":
    result = " "
failed = mode in {"agent_error", "labels_then_fail"}
emit({"type": "result", "subtype": "error_max_turns" if failed else "success", "is_error": failed,
      "result": result, "errors": ["Turn limit reached."] if failed else []})
sys.exit(1 if failed else 0)
