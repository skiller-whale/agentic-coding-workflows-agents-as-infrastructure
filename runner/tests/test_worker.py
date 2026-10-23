"""Worker integration checks: fake Claude and a test-owned Git repository."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace

import pytest
from werkzeug.serving import WSGIRequestHandler, make_server


RUNNER = Path(__file__).resolve().parents[1]
CODE = RUNNER.parent
sys.path.insert(0, str(RUNNER))
spec = importlib.util.spec_from_file_location("whalehub_worker", RUNNER / "worker.py")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)
app_spec = importlib.util.spec_from_file_location("worker_test_app", CODE / "whalehub" / "app.py")
app_module = importlib.util.module_from_spec(app_spec)
app_spec.loader.exec_module(app_module)


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()


@pytest.fixture
def exercise(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    files = {
        "README.md": "# Test repository\n",
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "src/example.py": 'def greet():\n    unused = None\n    return "Hello"\n',
        "tests/test_example.py": (
            "from src.example import greet\n\n"
            'def test_greet():\n    assert greet() == "Hello"\n'
        ),
        ".whalehub/workflow.md": (
            "---\nname: Saved workflow\nmodel: configured-test-model\n"
            "trigger: manual\ntools: []\n---\nSaved workflow instructions.\n"
        ),
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Runner test")
    git(repo, "config", "user.email", "runner@example.test")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Baseline")
    base = git(repo, "rev-parse", "HEAD")
    # User edits and staged files on main must survive coding worktrees.
    (repo / "README.md").write_text((repo / "README.md").read_text() + "\nUser edit.\n")
    git(repo, "add", "README.md")
    before = git(repo, "status", "--porcelain")
    cli = tmp_path / "fake-claude"
    shutil.copyfile(RUNNER / "tests" / "fake_claude.py", cli)
    cli.chmod(0o755)
    attendance = tmp_path / "attendance_id"
    attendance.write_text("test-attendance-no-network\n")
    calls, child_pid = tmp_path / "calls.jsonl", tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("FAKE_CHILD_PID", str(child_pid))
    config = worker.Config(repo=repo, runs=tmp_path / "runs", worktrees=tmp_path / "worktrees",
                           attendance=attendance, cli=str(cli), timeout=20)
    config.runs.mkdir()
    app = app_module.create_app({"TESTING": True, "EXECUTION_ENABLED": True,
                                 "REPO_PATH": repo, "RUNS_PATH": config.runs, "STATE_PATH": tmp_path / "state",
                                 "ISSUES_PATH": tmp_path / "whalehub/data/issues.json"})
    issues = app_module.read_json(tmp_path / "whalehub/data/issues.json")[:2]
    for issue in issues:
        issue.update(labels=[], comments=[])
    app_module.write_json(tmp_path / "whalehub/data/issues.json", issues)
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    config.api_url = f"http://127.0.0.1:{server.server_port}"

    def publish(output="summary", identifier="a" * 32, model="configured-test-model", **issue_changes):
        directory = config.runs / identifier
        directory.mkdir()
        job = {"run_id": identifier, "created_at": worker.now(), "base_commit": base,
               "workflow": {"name": "Snapshot workflow", "path": ".whalehub/workflow.md", "source": "Snapshot source",
                            "model": model, "trigger": "manual", "tools": ["SetIssueLabels"] if output == "summary" else ["Read", "Edit", "Bash"],
                            "output": output, "instructions": "Snapshot instructions: preserve existing behaviour."},
               "issue": {"id": 1, "title": "Snapshot title", "body": "Snapshot issue body", "labels": ["agent-ready"], **issue_changes}}
        worker.write_json(directory / "status.json", {"status": "running", "message": "Waiting."})
        worker.write_json(directory / "job.json", job)
        return directory

    try:
        yield SimpleNamespace(config=config, repo=repo, base=base, before=before, calls=calls,
                              child_pid=child_pid, publish=publish, client=app.test_client())
    finally:
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()


def run(exercise, monkeypatch, mode, output="summary"):
    monkeypatch.setenv("FAKE_MODE", mode)
    directory = exercise.publish(output)
    worker.Worker(exercise.config).run(once=True)
    return directory, worker.read_json(directory / "status.json")


def test_triage_snapshot_tools_credentials_stream_and_metadata(exercise, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "unrelated-profile")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "unrelated-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "unrelated-oauth")
    directory, status = run(exercise, monkeypatch, "triage")
    assert status["status"] == "succeeded"
    assert status["model"] == "resolved-test-model"
    assert status["requested_model"] == "configured-test-model"
    assert status["agent_exit_code"] == 0
    assert status["started_at"] < status["finished_at"]
    assert status["max_turns"] == 12
    assert exercise.client.get("/api/issues").json["issues"][0]["labels"] == ["maintenance", "agent-ready"]
    call = json.loads(exercise.calls.read_text())
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__whalehub__SetIssueLabels"
    assert "--strict-mcp-config" in argv
    assert "--bare" not in argv and "--safe-mode" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert worker.read_json(directory / "mcp.json")["mcpServers"]["whalehub"]["args"][-1] == str(directory)
    assert argv[argv.index("--max-turns") + 1] == "12"
    assert "Snapshot instructions" in argv[1] and "Snapshot issue body" in argv[1]
    assert "plain-text summary" not in argv[1]
    assert call["cwd"] != str(exercise.repo)
    assert "--json-schema" not in argv
    assert call["environment"]["AWS_ACCESS_KEY_ID"] == "test-attendance-no-network"
    assert call["environment"]["AWS_SECRET_ACCESS_KEY"] == "unused"
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert call["environment"][key] is None
    log = (directory / "run.log").read_text()
    assert log.count("I am inspecting the supplied issue.") == 1
    for text in ("[Tool mcp__whalehub__SetIssueLabels]", "Updated labels on issue #1", "Fixture diagnostic", "A small, clear cleanup."):
        assert text in log
    assert "test-attendance" not in log
    assert (directory / "claude.jsonl").exists()
    assert not (directory / "job.json").exists()
    assert (directory / "claimed.json").exists()


def test_empty_summary_is_allowed_but_missing_completion_fails(exercise, monkeypatch):
    directory, status = run(exercise, monkeypatch, "empty_summary")
    assert status["status"] == "succeeded"
    assert worker.read_json(directory / "result.json")["summary"] == ""
    Path(exercise.config.cli).write_text("#!/usr/bin/env python3\nprint('no completion event')\n")
    following = exercise.publish(identifier="b" * 32)
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(following / "status.json")["status"] == "failed"
    assert "did not complete" in worker.read_json(following / "status.json")["message"]


def test_summary_without_label_tool_does_not_change_labels(exercise, monkeypatch):
    directory = exercise.publish()
    job = worker.read_json(directory / "job.json")
    job["workflow"]["tools"] = []
    worker.write_json(directory / "job.json", job)
    monkeypatch.setenv("FAKE_MODE", "triage")
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "succeeded"
    assert worker.read_json(directory / "result.json")["summary"] == "A small, clear cleanup."
    assert not (directory / "labels.json").exists()
    call = json.loads(exercise.calls.read_text())
    assert "--allowedTools" not in call["argv"]
    assert worker.read_json(directory / "mcp.json") == {"mcpServers": {}}


@pytest.mark.parametrize("groups,native", [
    (["Read"], "Read,Glob,Grep"),
    (["Edit"], "Edit,Write,NotebookEdit"),
    (["Bash"], "Bash"),
    (["Subagents"], "Agent"),
    (["Read", "Edit", "Bash", "Subagents", "ReadIssue"],
     "Read,Glob,Grep,Edit,Write,NotebookEdit,Bash,Agent"),
])
def test_workflow_groups_select_and_approve_native_tools(exercise, monkeypatch, groups, native):
    directory = exercise.publish()
    job = worker.read_json(directory / "job.json")
    job["workflow"]["tools"] = groups
    worker.write_json(directory / "job.json", job)
    monkeypatch.setenv("FAKE_MODE", "empty_summary")
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "succeeded"
    argv = json.loads(exercise.calls.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == native
    allowed = native + (",mcp__whalehub__ReadIssue" if "ReadIssue" in groups else "")
    assert argv[argv.index("--allowedTools") + 1] == allowed
    assert worker.TOOLS == app_module.TOOLS


def test_label_tool_accepts_custom_labels_and_last_call_wins(exercise, monkeypatch):
    directory, status = run(exercise, monkeypatch, "labels_twice")
    assert status["status"] == "succeeded"
    assert exercise.client.get("/api/issues").json["issues"][0]["labels"] == ["custom-label"]
    assert (directory / "run.log").read_text().count("[Tool mcp__whalehub__SetIssueLabels]") == 2


def test_invalid_tool_arguments_return_error_without_recording_labels(exercise, monkeypatch):
    directory, status = run(exercise, monkeypatch, "invalid_labels")
    assert status["status"] == "succeeded"
    assert "labels" not in worker.read_json(directory / "result.json")
    assert not (directory / "labels.json").exists()
    assert "[Tool result error]" in (directory / "run.log").read_text()


def test_successful_tool_update_persists_when_agent_later_fails(exercise, monkeypatch):
    directory, status = run(exercise, monkeypatch, "labels_then_fail")
    assert status["status"] == "failed"
    assert (directory / "result.json").exists()
    assert exercise.client.get("/api/issues").json["issues"][0]["labels"] == ["maintenance", "agent-ready"]


def test_comment_only_workflow_and_issue_reference_rendering(exercise, monkeypatch):
    directory = exercise.publish(body="Preserve this literal {{ issue.id }} in the issue body.")
    job = worker.read_json(directory / "job.json")
    job["workflow"]["tools"] = ["AddIssueComment"]
    job["workflow"]["instructions"] = "Inspect issue #{{ issue.id }}. Read the comments too."
    job["issue"]["comments"] = [{"id": 1, "body": "Extra context from the user."}]
    worker.write_json(directory / "job.json", job)
    monkeypatch.setenv("FAKE_MODE", "comments")
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "succeeded"
    prompt = json.loads(exercise.calls.read_text())["argv"][1]
    assert "Inspect issue #1. Read the comments too." in prompt
    assert "Preserve this literal {{ issue.id }} in the issue body." in prompt
    assert "Extra context from the user." in prompt
    assert worker.read_json(directory / "claimed.json") == job
    issue = exercise.client.get("/api/issues").json["issues"][0]
    assert len(issue["comments"]) == 1  # The blank second tool call was rejected.
    assert issue["comments"][0]["body"] == "Reviewed the issue."
    assert issue["comments"][0]["run_id"] == directory.name
    assert issue["labels"] == []
    log = (directory / "run.log").read_text()
    assert "Posted comment #1 on issue #1" in log and "[Tool result error]" in log


@pytest.mark.parametrize("mode,expected_status,test_exit", [
    ("cleanup", "succeeded", 0), ("agent_error", "failed", 0),
    ("missing_result", "failed", 0), ("bad_tests", "succeeded", 2),
])
def test_agent_owns_tests_and_commits_while_runtime_collects(exercise, monkeypatch, mode, expected_status, test_exit):
    directory, status = run(exercise, monkeypatch, mode, "patch")
    assert status["status"] == expected_status
    result = worker.read_json(directory / "result.json")
    assert result["base_commit"] == exercise.base
    assert "test_exit_code" not in result
    checkout = Path(status["worktree"])
    assert git(checkout, "branch", "--show-current") == status["branch"]
    assert git(checkout, "rev-parse", "HEAD") == result["result_commit"]
    patch = (directory / "changes.patch").read_text()
    assert patch == subprocess.check_output(["git", "-C", str(checkout), "diff", "--binary", exercise.base, result["result_commit"]], text=True)
    if mode == "bad_tests":
        assert result["result_commit"] == exercise.base
        assert result["uncommitted_changes"] is True
        assert "-    unused = None" in (directory / "uncommitted.patch").read_text()
        assert git(checkout, "status", "--porcelain")
    else:
        assert result["result_commit"] != exercise.base
        assert "[fixture agent]" in git(checkout, "log", "-1", "--format=%s")
        assert "-    unused = None" in patch
        assert not git(checkout, "status", "--porcelain")
    assert git(exercise.repo, "branch", "--show-current") == "main"
    assert git(exercise.repo, "rev-parse", "HEAD") == exercise.base
    assert git(exercise.repo, "status", "--porcelain") == exercise.before
    log = (directory / "run.log").read_text()
    assert "[Verification]" not in log
    assert "[Runner] pytest exit code" not in log
    if test_exit == 0:
        assert "1 passed" in log
        assert "[Tool Bash]" in log and "pytest tests -q" in log and "git commit" in log
    argv = json.loads(exercise.calls.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,NotebookEdit,Bash"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep,Edit,Write,NotebookEdit,Bash"


@pytest.mark.parametrize("mode", ["uncommitted", "untracked", "partially_committed"])
def test_uncommitted_work_is_retained_without_automatic_commit_or_failure(exercise, monkeypatch, mode):
    directory, status = run(exercise, monkeypatch, mode, "patch")
    assert status["status"] == "succeeded"
    result = worker.read_json(directory / "result.json")
    assert result["uncommitted_changes"] is True
    pending = (directory / "uncommitted.patch").read_text()
    if mode == "partially_committed":
        assert result["result_commit"] != exercise.base
        assert "Unfinished follow-up edit" in pending
        assert "Unfinished follow-up edit" not in (directory / "changes.patch").read_text()
    else:
        assert result["result_commit"] == exercise.base
        assert (directory / "changes.patch").read_text() == ""
        assert "new_feature.py" in pending if mode == "untracked" else "-    unused = None" in pending
    assert git(exercise.repo, "status", "--porcelain") == exercise.before


def test_coding_runtime_handles_a_readme_only_change(exercise, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "alternate_issue")
    directory = exercise.publish("patch", id=17, title="Document CLI usage", body="Add a short CLI usage note to README.")
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "succeeded"
    result = worker.read_json(directory / "result.json")
    assert git(Path(worker.read_json(directory / "status.json")["worktree"]), "show", "--format=", "--name-only", result["result_commit"]) == "README.md"
    assert "src/example.py" not in (directory / "changes.patch").read_text()


def test_coding_no_changes_has_no_empty_commit(exercise, monkeypatch):
    directory, status = run(exercise, monkeypatch, "no_changes", "patch")
    result = worker.read_json(directory / "result.json")
    assert status["status"] == "succeeded"
    assert result["result_commit"] == result["base_commit"] == exercise.base
    assert (directory / "changes.patch").read_text() == ""
    assert "1 passed" in (directory / "run.log").read_text()


def test_issue_labels_do_not_control_execution(exercise, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "no_changes")
    directory = exercise.publish("patch", labels=["needs-human"])
    worker.Worker(exercise.config).run(once=True)
    status = worker.read_json(directory / "status.json")
    assert status["status"] == "succeeded"
    assert exercise.calls.exists() and Path(status["worktree"]).is_dir()


def test_empty_attendance_never_invokes_cli(exercise):
    exercise.config.attendance.write_text("")
    directory = exercise.publish()
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "failed"
    assert "Attendance ID is empty" in (directory / "run.log").read_text()
    assert not exercise.calls.exists()


def test_each_job_uses_its_snapshotted_model_over_environment_defaults(exercise, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "unrelated-environment-model")
    models = ["eu.anthropic.claude-haiku-5-5", "eu.anthropic.claude-sonnet-5-5"]
    jobs = [exercise.publish(identifier=letter * 32, model=model)
            for letter, model in zip("ab", models)]
    worker.Worker(exercise.config).run(once=True)
    calls = [json.loads(line) for line in exercise.calls.read_text().splitlines()]
    for job, call, model in zip(jobs, calls, models):
        argv = call["argv"]
        assert argv[argv.index("--model") + 1] == model
        assert call["environment"]["ANTHROPIC_MODEL"] == model
        assert worker.read_json(job / "status.json")["requested_model"] == model


def test_each_job_uses_its_limits_without_changing_runner_defaults(exercise, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "empty_summary")
    exercise.config.max_turns = 7
    jobs = []
    overrides = [{"max_turns": 20, "timeout_seconds": 300}, {"max_turns": 3}, {"timeout_seconds": 1}, {}]
    for letter, limits in zip("abcd", overrides):
        directory = exercise.publish(identifier=letter * 32)
        job = worker.read_json(directory / "job.json")
        job["workflow"].update(tools=[], **limits)
        worker.write_json(directory / "job.json", job)
        jobs.append(directory)
    actual_timeouts = []
    original = worker.run_process
    def record_timeout(command, cwd, env, timeout, stopped, consume):
        actual_timeouts.append(timeout)
        return original(command, cwd, env, timeout, stopped, consume)
    monkeypatch.setattr(worker, "run_process", record_timeout)
    worker.Worker(exercise.config).run(once=True)
    calls = [json.loads(line) for line in exercise.calls.read_text().splitlines()]
    expected = [(20, 300), (3, 20), (7, 1), (7, 20)]
    assert len(calls) == 4
    assert actual_timeouts == [timeout for _, timeout in expected]
    for directory, call, (turns, timeout) in zip(jobs, calls, expected):
        status = worker.read_json(directory / "status.json")
        assert status["status"] == "succeeded"
        assert status["max_turns"] == turns and status["timeout_seconds"] == timeout
        assert call["argv"][call["argv"].index("--max-turns") + 1] == str(turns)
        log = (directory / "run.log").read_text()
        assert f"max turns={turns}; timeout={timeout}s" in log
    assert exercise.config.max_turns == 7 and exercise.config.timeout == 20


@pytest.mark.parametrize("field", ["max_turns", "timeout_seconds"])
@pytest.mark.parametrize("value", [0, True])
def test_invalid_job_limits_fail_before_preparation_or_cli(exercise, field, value):
    directory = exercise.publish()
    job = worker.read_json(directory / "job.json")
    job["workflow"][field] = value
    worker.write_json(directory / "job.json", job)
    worker.Worker(exercise.config).run(once=True)
    status = worker.read_json(directory / "status.json")
    assert status["status"] == "failed" and field in status["message"]
    assert not exercise.calls.exists()
    assert not (exercise.config.worktrees / directory.name).exists()


def test_missing_workflow_model_fails_without_cli_or_fallback(exercise, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "unrelated-environment-model")
    directory = exercise.publish(model=None)
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "failed"
    assert "model ID" in (directory / "run.log").read_text()
    assert not exercise.calls.exists()


def assert_child_stopped(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True)
    assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("timeout_override", [None, 1])
def test_agent_wall_timeout_kills_children_and_retains_uncommitted_work(exercise, monkeypatch, timeout_override):
    exercise.config.timeout = 0.3 if timeout_override is None else 20
    directory = exercise.publish("patch")
    if timeout_override is not None:
        job = worker.read_json(directory / "job.json")
        job["workflow"]["timeout_seconds"] = timeout_override
        worker.write_json(directory / "job.json", job)
    monkeypatch.setenv("FAKE_MODE", "timeout")
    worker.Worker(exercise.config).run(once=True)
    status = worker.read_json(directory / "status.json")
    assert status["status"] == "failed" and "Timed out" in status["message"]
    assert status["timeout_seconds"] == (timeout_override or exercise.config.timeout)
    assert worker.read_json(directory / "result.json")["result_commit"] == exercise.base
    assert "-    unused = None" in (directory / "uncommitted.patch").read_text()
    assert_child_stopped(int(exercise.child_pid.read_text()))


def test_runner_does_not_run_tests_implicitly(exercise, monkeypatch):
    git(exercise.repo, "restore", "--staged", "README.md")
    (exercise.repo / "tests/test_slow.py").write_text("raise RuntimeError('runner must not run this test')\n")
    git(exercise.repo, "add", "tests/test_slow.py")
    git(exercise.repo, "commit", "-m", "Existing test")
    directory = exercise.publish("patch")
    job = worker.read_json(directory / "job.json")
    job["base_commit"] = git(exercise.repo, "rev-parse", "HEAD")
    worker.write_json(directory / "job.json", job)
    monkeypatch.setenv("FAKE_MODE", "commit_without_tests")
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / "status.json")["status"] == "succeeded"
    assert (directory / "changes.patch").read_text()
    assert "pytest tests" not in (directory / "run.log").read_text()


def test_restart_never_replays_claimed_jobs_and_ignores_pending_directories(exercise, monkeypatch):
    interrupted = exercise.publish(identifier="b" * 32)
    (interrupted / "job.json").rename(interrupted / "claimed.json")
    worker.write_json(interrupted / "status.json", {"status": "running", "model": "previous-model"})
    (interrupted / "run.log").write_text("Partial agent output.\n")
    worker.write_json(interrupted / "result.json", {"partial": "retained"})
    pending = exercise.config.runs / ".pending-ignore"
    pending.mkdir()
    (pending / "job.json").write_text("invalid JSON ignored")
    done, _ = run(exercise, monkeypatch, "triage")
    status = worker.read_json(interrupted / "status.json")
    assert status["status"] == "failed" and "not be replayed" in status["message"]
    assert status["model"] == "previous-model"
    assert worker.read_json(interrupted / "result.json") == {"partial": "retained"}
    assert "Partial agent output." in (interrupted / "run.log").read_text()
    worker.Worker(exercise.config).run(once=True)
    assert len(exercise.calls.read_text().splitlines()) == 1
    assert worker.read_json(done / "status.json")["status"] == "succeeded"


def test_second_worker_cannot_claim_or_recover_active_worker_jobs(exercise):
    directory = exercise.publish()
    with (exercise.config.runs / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another worker"):
            worker.Worker(exercise.config).run(once=True)
    assert (directory / "job.json").exists()
    assert not (directory / "claimed.json").exists()


def test_sigterm_stops_agent_group_and_finishes_failed_run(exercise, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "shutdown")
    directory = exercise.publish("patch")
    config = exercise.config
    env = {**os.environ, "WHALEHUB_REPO": str(config.repo), "WHALEHUB_RUNS": str(config.runs),
           "WHALEHUB_WORKTREES": str(config.worktrees), "WHALEHUB_ATTENDANCE_FILE": str(config.attendance),
           "WHALEHUB_CLAUDE_BIN": config.cli}
    process = subprocess.Popen([sys.executable, str(RUNNER / "worker.py")], env=env)
    try:
        deadline = time.monotonic() + 10
        while not exercise.child_pid.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert exercise.child_pid.exists()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    status = worker.read_json(directory / "status.json")
    assert status["status"] == "failed" and "shutdown" in status["message"]
    assert (directory / "changes.patch").exists()
    assert_child_stopped(int(exercise.child_pid.read_text()))


def test_stop_waiting_run_never_invokes_claude(exercise):
    directory = exercise.publish()
    response = exercise.client.post('/api/runs/' + directory.name + '/stop')
    assert response.status_code == 202 and response.json['status'] == 'stopped'
    worker.Worker(exercise.config).run(once=True)
    assert worker.read_json(directory / 'status.json')['status'] == 'stopped'
    assert not exercise.calls.exists()
    assert not (exercise.config.worktrees / directory.name).exists()


@pytest.mark.parametrize('test', [False, True])
def test_stop_active_run_or_test_kills_children_and_retains_artifacts(exercise, monkeypatch, test):
    monkeypatch.setenv('FAKE_MODE', 'shutdown')
    if test:
        source = ('---\nname: Unsaved demo\nmodel: configured-test-model\ntrigger: manual\n'
                  'tools: []\n---\n\nInspect the demo issue.\n')
        response = exercise.client.post('/api/workflow-tests', json={
            'workflow_path': '.whalehub/workflow.md', 'source': source,
            'issue': {'title': 'Demo', 'body': 'Demo body'}})
        assert response.status_code == 202, response.json
        identifier = response.json['run_id']
    else:
        identifier = exercise.publish('patch').name
    directory = exercise.config.runs / identifier
    errors = []
    def execute():
        try:
            worker.Worker(exercise.config).run(once=True)
        except Exception as error:
            errors.append(error)
    thread = Thread(target=execute, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not exercise.child_pid.exists() and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert exercise.child_pid.exists(), errors
    endpoint = ('/api/workflow-tests/' if test else '/api/runs/') + identifier
    response = exercise.client.post(endpoint + '/stop')
    assert response.status_code == 202, response.json
    thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    status = worker.read_json(directory / 'status.json')
    assert status['status'] == 'stopped' and status['finished_at']
    assert 'Stopped by user.' in status['message']
    assert (directory / 'changes.patch').exists()
    assert (directory / 'result.json').exists()
    assert_child_stopped(int(exercise.child_pid.read_text()))
    if test:
        assert exercise.client.get(endpoint).json['status'] == 'stopped'


def test_no_issue_workflow_reads_creates_and_updates_issues_via_mcp(exercise, monkeypatch):
    directory = exercise.publish()
    job = worker.read_json(directory / 'job.json')
    job.pop('issue')
    job['event'] = {}
    job['workflow'].pop('output')
    job['workflow']['tools'] = ['ReadIssues', 'CreateIssue', 'SetIssueLabels', 'AddIssueComment']
    worker.write_json(directory / 'job.json', job)
    monkeypatch.setenv('FAKE_MODE', 'issue_admin')
    worker.Worker(exercise.config).run(once=True)
    status = worker.read_json(directory / 'status.json')
    assert status['status'] == 'succeeded', (status, (directory / 'run.log').read_text())
    issues = exercise.client.get('/api/issues').json['issues']
    assert issues[2]['title'] == 'New agent task'
    assert issues[2]['labels'] == ['new-task']
    assert issues[2]['comments'][0]['run_id'] == directory.name
    assert worker.read_json(directory / 'claimed.json')['event'] == {}
    assert Path(json.loads(exercise.calls.read_text())['cwd']) == Path(status['worktree'])
    assert git(exercise.repo, 'status', '--porcelain') == exercise.before


def test_single_issue_workflow_reads_current_contents_via_mcp(exercise, monkeypatch):
    directory = exercise.publish()
    job = worker.read_json(directory / 'job.json')
    job['workflow']['tools'] = ['ReadIssue']
    worker.write_json(directory / 'job.json', job)
    assert exercise.client.put('/api/issues/1', json={'title': 'Updated single issue', 'body': 'Current body.'}).status_code == 200
    assert exercise.client.post('/api/issues/1/comments', json={'body': 'Current discussion.'}).status_code == 201
    monkeypatch.setenv('FAKE_MODE', 'single_issue')
    worker.Worker(exercise.config).run(once=True)
    status = worker.read_json(directory / 'status.json')
    assert status['status'] == 'succeeded', (status, (directory / 'run.log').read_text())
    argv = json.loads(exercise.calls.read_text())['argv']
    assert argv[argv.index('--allowedTools') + 1] == 'mcp__whalehub__ReadIssue'
    assert '[Tool mcp__whalehub__ReadIssue]' in (directory / 'run.log').read_text()


def test_event_fields_are_rendered_once_and_missing_issue_is_empty():
    event = {'issue': {'number': 8, 'title': 'Literal {{ whalehub.event.issue.number }}', 'labels': ['custom'], 'comments': []}}
    assert worker.render_instructions('#{{ whalehub.event.issue.number }} {{ whalehub.event.issue.title }}', event) == '#8 Literal {{ whalehub.event.issue.number }}'
    assert worker.render_instructions('{{ whalehub.event.issue.labels }}', event) == '["custom"]'
    assert worker.render_instructions('{{ whalehub.event.issue.comments }}', event) == '[]'
    assert worker.render_instructions('{{ whalehub.event.issue.number }}', {}) == ''
    assert worker.render_instructions('{{ issue.id }}', event) == '8'


@pytest.mark.parametrize('mode', ['triage', 'issue_admin', 'labels_then_fail', 'untracked'])
def test_unsaved_workflow_tests_use_real_runner_and_keep_issue_changes_private(exercise, monkeypatch, mode):
    path = '.whalehub/workflow.md'
    saved = (exercise.repo / path).read_bytes()
    before = exercise.client.get('/api/issues').json
    source = ('---\nname: Unsaved demo\nmodel: configured-test-model\ntrigger: issue_created\n'
              'tools: [Read, Edit, Bash, ReadIssue, ReadIssues, CreateIssue, SetIssueLabels, AddIssueComment]\n---\n'
              'These are unsaved instructions for demo issue #{{ whalehub.event.issue.number }}.\n')
    response = exercise.client.post('/api/workflow-tests', json={
        'workflow_path': path, 'source': source, 'issue': {'title': 'Welcome typo', 'body': 'Change Welcom to Welcome.'}})
    assert response.status_code == 202, response.json
    identifier = response.json['run_id']
    monkeypatch.setenv('FAKE_MODE', mode)
    worker.Worker(exercise.config).run(once=True)
    detail = exercise.client.get('/api/workflow-tests/' + identifier).json
    assert detail['status'] == ('failed' if mode == 'labels_then_fail' else 'succeeded'), detail
    assert detail['source'] == source
    if mode == 'issue_admin':
        assert len(detail['issues']) == 2
        assert detail['issues'][1]['labels'] == ['new-task']
        assert detail['issues'][1]['comments'][0]['body'] == 'Created and labelled by the agent.'
    elif mode == 'untracked':
        assert detail['result']['uncommitted_changes'] is True
        assert 'src/new_feature.py' in detail['uncommitted_patch']
    else:
        assert detail['issues'][0]['labels'] == ['maintenance', 'agent-ready']
        assert 'Triage report' in detail['issues'][0]['comments'][0]['body']
    assert exercise.client.get('/api/issues').json == before
    assert exercise.client.get('/api/runs').json == {'runs': [], 'busy': False}
    argv = json.loads(exercise.calls.read_text())['argv']
    assert 'These are unsaved instructions for demo issue #1.' in argv[argv.index('--print') + 1]
    assert (exercise.repo / path).read_bytes() == saved
    assert git(exercise.repo, 'status', '--porcelain') == exercise.before
