"""Exercise boundaries in disposable repositories; never initialise the source tree."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("whalehub_app", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
WORKFLOW = "---\nname: Inspect issue\nmodel: eu.anthropic.claude-haiku-5-5\ntrigger: manual\ntools: [SetIssueLabels]\n---\n\nRead and classify the supplied issue.\n"
TRIAGE = {"labels": ["maintenance", "agent-ready"], "summary": "A small cleanup."}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def exercise(tmp_path):
    repo = tmp_path / "repository"
    scaffold = WORKFLOW.replace("Read and classify the supplied issue.", "TODO: Write the workflow instructions.")
    files = {
        "README.md": "# Test repository\n",
        "requirements.txt": "",
        "src/example.py": "VALUE = 1\n",
        "tests/fixtures/example.md": "# Test fixture\n",
        "samples/example.md": "# Sample document\n",
        ".whalehub/triage.md": scaffold,
        ".whalehub/coding.md": scaffold.replace("[SetIssueLabels]", "[Read, Edit, Bash]"),
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Whalehub Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Baseline")
    config = {"TESTING": True, "REPO_PATH": repo, "STATE_PATH": tmp_path / "state", "RUNS_PATH": tmp_path / "runs",
              "ISSUES_PATH": tmp_path / "whalehub/data/issues.json"}
    app = module.create_app(config)
    issues = module.read_json(config["ISSUES_PATH"])[:2]
    for issue in issues:
        issue.update(labels=[], comments=[])
    module.write_json(config["ISSUES_PATH"], issues)
    return app, app.test_client(), repo, config


def save(client, source=WORKFLOW, path=".whalehub/triage.md", **extra):
    return client.put("/api/workflows", json={"path": path, "source": source, **extra})


def publish(exercise, workflow=".whalehub/triage.md", issue=1):
    app, client, _, config = exercise
    app.config["EXECUTION_ENABLED"] = True
    response = client.post("/api/runs", json={"workflow_path": workflow, "issue_id": issue})
    assert response.status_code == 202, response.json
    return config["RUNS_PATH"] / response.json["run_id"]


def set_labels(client, path, labels):
    if (path / "job.json").exists():
        (path / "job.json").rename(path / "claimed.json")
    return client.post("/api/runs/" + path.name + "/labels", json={"issue_number": 1, "labels": labels})


def finish(path, result=TRIAGE, status="succeeded"):
    module.write_json(path / "result.json", result)
    module.write_json(path / "status.json", {"status": status, "message": "Fixture worker completed."})


@pytest.mark.parametrize("test", [False, True])
def test_stop_waiting_workflow_releases_busy_state_and_keeps_routes_separate(exercise, test):
    app, client, _, config = exercise
    app.config["EXECUTION_ENABLED"] = True
    if test:
        response = client.post("/api/workflow-tests", json={
            "workflow_path": ".whalehub/triage.md", "source": WORKFLOW,
            "issue": {"title": "Demo", "body": "Demo body"}})
    else:
        response = client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1})
    assert response.status_code == 202, response.json
    identifier = response.json["run_id"]
    path = config["RUNS_PATH"] / identifier
    endpoint = ("/api/workflow-tests/" if test else "/api/runs/") + identifier
    other = ("/api/runs/" if test else "/api/workflow-tests/") + identifier
    assert client.post(other + "/stop").status_code == 404
    stopped = client.post(endpoint + "/stop")
    assert stopped.status_code == 202 and stopped.json["status"] == "stopped"
    assert client.post(endpoint + "/stop").status_code == 409
    assert client.get(endpoint).json["status"] == "stopped"
    assert (path / "job.json").exists() and not (path / "claimed.json").exists()
    assert client.get("/api/runs").json["busy"] is False
    assert client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1}).status_code == 202


def test_stop_claimed_run_blocks_agent_issue_tools(exercise):
    _, client, _, _ = exercise
    path = publish(exercise)
    (path / "job.json").rename(path / "claimed.json")
    prefix = "/api/runs/" + path.name
    assert client.post(prefix + "/stop").status_code == 202
    detail = client.get(prefix).json
    assert detail["status"] == "running" and detail["stop_requested"] is True
    assert detail["message"] == "Stopping workflow…"
    assert client.post(prefix + "/labels", json={"issue_number": 1, "labels": ["new"]}).status_code == 409


def test_code_browser_boundaries(exercise, tmp_path):
    _, client, repo, _ = exercise
    (repo / "src" / "__pycache__").mkdir()
    (repo / "src" / "__pycache__" / "hidden.py").write_text("private")
    (repo / "src" / "dist").mkdir()
    (repo / "src" / "dist" / "generated.py").write_text("generated")
    (repo / "src" / "outside.py").symlink_to(tmp_path / "state" / "issues.json")
    (repo / "tests" / "escape").symlink_to(tmp_path / "state", target_is_directory=True)
    (repo / ".whalehub" / "linked.md").symlink_to(repo / "README.md")
    (repo / ".whalehub" / "private.json").write_text("private")
    (repo / "setup.sh").write_text("setup")
    files = client.get("/api/files").json["files"]
    assert "src/example.py" in files
    assert "tests/fixtures/example.md" in files
    assert "samples/example.md" in files
    assert {"README.md", "requirements.txt", ".whalehub/triage.md"} <= set(files)
    for path in ["../whalehub/app.py", "/etc/passwd", "src/../../app.py", "src/./example.py",
                 "src//example.py", "src\\example.py", ".git/config", "setup.sh", "attendance_id",
                 ".whalehub/private.json", "src/__pycache__/hidden.py", "src/dist/generated.py",
                 "src/outside.py", "tests/escape/issues.json", ".whalehub/linked.md"]:
        assert path not in files
        assert client.get("/api/files", query_string={"path": path}).status_code == 404, path
    assert client.get("/api/files", query_string={"path": "src/example.py"}).json["source"]


def test_workflow_directory_symlink_is_hidden(exercise, tmp_path):
    _, client, repo, _ = exercise
    (repo / ".whalehub").rename(tmp_path / "workflows")
    (repo / ".whalehub").symlink_to(tmp_path / "workflows", target_is_directory=True)
    assert client.get("/api/workflows").json == {"workflows": []}
    assert not any(p.startswith(".whalehub/") for p in client.get("/api/files").json["files"])
    assert save(client).status_code == 404


def test_discovers_unknown_workflow_and_saves_its_full_text(exercise):
    _, client, repo, _ = exercise
    path = ".whalehub/user-choice.md"
    (repo / path).write_text(WORKFLOW)
    assert path in [w["path"] for w in client.get("/api/workflows").json["workflows"]]
    updated = WORKFLOW + "\n## User instructions\nKeep this entire document.\n"
    assert save(client, updated, path).json["committed"] is True
    assert git(repo, "show", "HEAD:" + path) == updated.strip()
    assert (repo / path).read_text() == updated


def test_workflow_groups_save_and_snapshot_with_individual_issue_tools(exercise):
    _, client, repo, _ = exercise
    tools = ["Read", "Edit", "Bash", "Subagents", "ReadIssue", "AddIssueComment"]
    source = WORKFLOW.replace("[SetIssueLabels]", "[" + ", ".join(tools) + "]")
    assert save(client, source).status_code == 200
    assert (repo / ".whalehub/triage.md").read_text() == source
    directory = publish(exercise)
    assert module.read_json(directory / "job.json")["workflow"]["tools"] == tools


@pytest.mark.parametrize("trigger", ["manual", "issue_created"])
def test_workflow_runtime_limits_are_saved_and_snapshotted(exercise, trigger):
    app, client, _, config = exercise
    app.config["EXECUTION_ENABLED"] = True
    source = WORKFLOW.replace("trigger: manual", f"trigger: {trigger}\nmax_turns: 20\ntimeout_seconds: 300")
    assert save(client, source).status_code == 200
    if trigger == "manual":
        directory = publish(exercise)
    else:
        created = client.post("/api/issues", json={"title": "New task", "body": "Use the configured limits."})
        assert created.status_code == 201
        directory = config["RUNS_PATH"] / created.json["triggered_runs"][0]["run_id"]
    job = module.read_json(directory / "job.json")
    assert job["workflow"]["max_turns"] == 20
    assert job["workflow"]["timeout_seconds"] == 300
    assert save(client, WORKFLOW).status_code == 200
    assert module.read_json(directory / "job.json") == job


@pytest.mark.parametrize("field", ["max_turns", "timeout_seconds"])
@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", '"8"', "null"])
def test_invalid_runtime_limits_do_not_change_the_workflow(exercise, field, value):
    _, client, repo, _ = exercise
    before = (repo / ".whalehub/triage.md").read_text()
    head = git(repo, "rev-parse", "HEAD")
    source = WORKFLOW.replace("trigger: manual", f"trigger: manual\n{field}: {value}")
    response = save(client, source)
    assert response.status_code == 400
    assert response.json["error"] == f"{field} must be a positive integer when supplied."
    assert (repo / ".whalehub/triage.md").read_text() == before
    assert git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("source, message", [
    ("plain text", "frontmatter"),
    ("---\nname: [bad\n---\nBody", "Invalid YAML"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: []\nname: duplicate"), "Duplicate"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: []\nsteps: surprise"), "unsupported"),
    (WORKFLOW.replace("model: eu.anthropic.claude-haiku-5-5\n", ""), "missing"),
    (WORKFLOW.replace("model: eu.anthropic.claude-haiku-5-5", "model: 5"), "model"),
    (WORKFLOW.replace("model: eu.anthropic.claude-haiku-5-5", "model: ''"), "model"),
    (WORKFLOW.replace("model: eu.anthropic.claude-haiku-5-5", "model: 'model with spaces'"), "model"),
    (WORKFLOW.replace("tools: [SetIssueLabels]\n", ""), "missing"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [Unknown]"), "supported tools"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [Agent]"), "Subagents"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [Web]"), "supported tools"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [WebFetch]"), "supported tools"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [Read, Read]"), "distinct"),
    (WORKFLOW.replace("tools: [SetIssueLabels]", "tools: [[Read]]"), "supported tools"),
    (WORKFLOW.replace("trigger: manual", "trigger: automatic"), "manual"),
    (WORKFLOW.replace("trigger: manual", "trigger: [manual, issue_created]"), "trigger"),
    (WORKFLOW.replace("trigger: manual", "trigger: manual\noutput: mystery"), "output"),
    (WORKFLOW.replace("name: Inspect issue", "name: 5"), "name"),
    (WORKFLOW + "\nRead issue {{ whalehub.event.issue.unknown }}.", "Unknown workflow expression"),
    (WORKFLOW.split("---\n\n")[0] + "---\n\n", "instructions"),
])
def test_useful_workflow_validation_errors(exercise, source, message):
    _, client, repo, _ = exercise
    before = (repo / ".whalehub/triage.md").read_text()
    response = save(client, source)
    assert response.status_code == 400
    assert message in response.json["error"]
    assert (repo / ".whalehub/triage.md").read_text() == before


def test_workflow_commit_preserves_other_user_work(exercise):
    _, client, repo, _ = exercise
    source = repo / "src/example.py"
    source.write_text(source.read_text() + "\n# staged user edit\n")
    git(repo, "add", "src/example.py")
    staged = git(repo, "diff", "--cached", "--binary")
    source.write_text(source.read_text() + "\n# unstaged user edit\n")
    unstaged = git(repo, "diff", "--binary")
    (repo / "user-notes.txt").write_text("keep this")
    response = save(client)
    assert response.status_code == 200
    assert response.json["committed"] is True
    assert git(repo, "show", "--format=", "--name-only", "HEAD") == ".whalehub/triage.md"
    assert git(repo, "diff", "--cached", "--binary") == staged
    assert git(repo, "diff", "--binary") == unstaged
    assert (repo / "user-notes.txt").read_text() == "keep this"
    head = git(repo, "rev-parse", "HEAD")
    assert save(client).json["committed"] is False
    assert git(repo, "rev-parse", "HEAD") == head


def test_workflow_save_preserves_file_owner_and_permissions(exercise):
    _, client, repo, _ = exercise
    path = repo / ".whalehub/triage.md"
    path.chmod(0o640)
    before = path.stat()
    assert save(client).status_code == 200
    after = path.stat()
    assert (after.st_mode & 0o777) == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_non_main_and_stale_editor_saves_are_refused(exercise):
    _, client, repo, _ = exercise
    opened = client.get("/api/files?path=.whalehub/triage.md").json
    (repo / ".whalehub/triage.md").write_text(WORKFLOW)
    response = save(client, sha256=opened["sha256"])
    assert response.status_code == 409
    assert "changed since" in response.json["error"]
    git(repo, "switch", "-c", "user-branch")
    response = save(client, WORKFLOW + "\nExtra.\n")
    assert response.status_code == 409
    assert "main" in response.json["error"]
    assert git(repo, "branch", "--show-current") == "user-branch"
    assert (repo / ".whalehub/triage.md").read_text() == WORKFLOW


def test_file_backed_issues_are_unlabelled_and_not_reset_on_restart(exercise):
    _, client, _, config = exercise
    issues = client.get("/api/issues").json["issues"]
    assert len(issues) == 2
    assert all(i["labels"] == [] and set(i) == {"id", "title", "body", "labels", "comments"} for i in issues)
    issues[0]["labels"] = TRIAGE["labels"]
    module.write_json(config["ISSUES_PATH"], issues)
    restarted = module.create_app(config).test_client()
    assert restarted.get("/api/issues").json["issues"] == issues


def test_users_create_edit_and_comment_with_file_persistence(exercise):
    _, client, _, config = exercise
    response = client.post("/api/issues", json={"title": " Another task ", "body": "Please update the README."})
    assert response.status_code == 201
    issue = response.json
    assert issue == {"id": 3, "title": "Another task", "body": "Please update the README.",
                     "labels": [], "comments": [], "triggered_runs": []}
    comment = client.post("/api/issues/3/comments", json={"body": "Keep it short."})
    assert comment.status_code == 201
    assert comment.json["author"] == "User" and comment.json["id"] == 1
    assert "run_id" not in comment.json
    edited = client.put("/api/issues/3", json={"title": "README task", "body": "Clarify the CLI example."})
    assert edited.status_code == 200
    assert edited.json["comments"] == [comment.json]
    restarted = module.create_app(config).test_client()
    assert restarted.get("/api/issues").json["issues"][-1] == edited.json
    assert module.read_json(config["ISSUES_PATH"])[-1] == edited.json
    assert not (config["STATE_PATH"] / "issues.json").exists()


def test_old_comment_author_label_is_updated_without_changing_discussion(exercise):
    _, _, _, config = exercise
    issues = module.read_json(config["ISSUES_PATH"])
    comment = {"id": 1, "author": "Learner", "body": "Keep this discussion.", "created_at": "2026-10-19T10:00:00+00:00"}
    agent_comment = {**comment, "id": 2, "author": "Review issue", "run_id": "a" * 32}
    issues[0]["comments"] = [comment, agent_comment]
    module.write_json(config["ISSUES_PATH"], issues)
    restarted = module.create_app(config).test_client()
    expected = [{**comment, "author": "User"}, agent_comment]
    assert restarted.get("/api/issues").json["issues"][0]["comments"] == expected
    assert module.read_json(config["ISSUES_PATH"])[0]["comments"] == expected
    saved = config["ISSUES_PATH"].read_bytes()
    module.create_app(config)
    assert config["ISSUES_PATH"].read_bytes() == saved


@pytest.mark.parametrize("path,method,data,status", [
    ("/api/issues", "post", {"title": "", "body": "Body"}, 400),
    ("/api/issues", "post", {"title": "Title", "body": 123}, 400),
    ("/api/issues", "post", {"title": "Title", "body": "Body", "labels": ["agent-ready"]}, 400),
    ("/api/issues/99", "put", {"title": "Title", "body": "Body"}, 404),
    ("/api/issues/99/comments", "post", {"body": "Comment"}, 404),
    ("/api/issues/1/comments", "post", {"body": " "}, 400),
    ("/api/issues/1/comments", "post", {"body": "Comment", "author": "Agent"}, 400),
])
def test_issue_mutation_validation(exercise, path, method, data, status):
    _, client, _, _ = exercise
    before = client.get("/api/issues").json
    assert getattr(client, method)(path, json=data).status_code == status
    assert client.get("/api/issues").json == before


def test_concurrent_issue_and_comment_ids_are_unique(exercise):
    app, client, _, _ = exercise
    def create(index):
        with app.test_client() as peer:
            return peer.post("/api/issues", json={"title": f"Task {index}", "body": "Body"}).json["id"]
    def comment(index):
        with app.test_client() as peer:
            return peer.post("/api/issues/1/comments", json={"body": f"Comment {index}"}).json["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(create, range(4))) == [3, 4, 5, 6]
        assert sorted(pool.map(comment, range(4))) == [1, 2, 3, 4]
    assert len(client.get("/api/issues").json["issues"][0]["comments"]) == 4


def test_agent_comment_is_explicit_bound_to_run_and_part_of_future_snapshots(exercise):
    _, client, repo, _ = exercise
    (repo / ".whalehub/triage.md").write_text(WORKFLOW.replace("[SetIssueLabels]", "[AddIssueComment]"))
    client.post("/api/issues/1/comments", json={"body": "Please preserve the CLI."})
    path = publish(exercise)
    original = module.read_json(path / "job.json")
    assert original["event"]["issue"]["comments"][0]["body"] == "Please preserve the CLI."
    url = f"/api/runs/{path.name}/comments"
    assert client.post(url, json={"issue_number": 1, "body": "Report"}).status_code == 409
    (path / "job.json").rename(path / "claimed.json")
    assert client.post(url, json={"issue_number": 1, "body": "Report", "issue_id": 2}).status_code == 400
    response = client.post(url, json={"issue_number": 1, "body": "Small cleanup: maintenance and agent-ready."})
    assert response.status_code == 201
    comment = response.json["comment"]
    assert response.json["issue_number"] == 1
    assert comment["author"] == "Inspect issue" and comment["run_id"] == path.name
    assert comment["id"] == 2
    assert module.read_json(path / "claimed.json") == original
    finish(path, status="failed")
    assert client.post(url, json={"issue_number": 1, "body": "Too late"}).status_code == 409
    issues = client.get("/api/issues").json["issues"]
    assert issues[0]["comments"][-1] == comment and issues[1]["comments"] == []
    following = publish(exercise)
    assert module.read_json(following / "job.json")["event"]["issue"]["comments"][-1] == comment


def test_workflow_without_comment_tool_cannot_post_comments(exercise):
    _, client, _, _ = exercise
    path = publish(exercise)
    (path / "job.json").rename(path / "claimed.json")
    response = client.post(f"/api/runs/{path.name}/comments", json={"issue_number": 1, "body": "Report"})
    assert response.status_code == 403
    assert "AddIssueComment" in response.json["error"]
    assert client.get("/api/issues").json["issues"][0]["comments"] == []


def test_disabled_execution_is_honest(exercise):
    app, client, _, config = exercise
    app.config["EXECUTION_ENABLED"] = False
    assert client.get("/").status_code == 200
    health = client.get("/health").json
    assert health["workflow_execution"] is False
    assert health["job_publication"] is False
    assert health["worker"] == "disabled"
    response = client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1})
    assert response.status_code == 503
    assert "disabled" in response.json["error"]
    assert not list(config["RUNS_PATH"].iterdir())


def test_environment_enables_job_publication_for_worker(exercise, monkeypatch):
    _, _, _, config = exercise
    monkeypatch.setenv("WHALEHUB_EXECUTION_ENABLED", "1")
    client = module.create_app(config).test_client()
    health = client.get("/health").json
    assert health["workflow_execution"] is True
    assert health["worker"] == "configured"
    assert b'data-execution="true"' in client.get("/").data
    response = client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1})
    assert response.status_code == 202
    assert (config["RUNS_PATH"] / response.json["run_id"] / "job.json").exists()


def test_publication_snapshots_and_one_active_rule(exercise):
    _, client, repo, config = exercise
    path = publish(exercise)
    job = module.read_json(path / "job.json")
    assert job["base_commit"] == git(repo, "rev-parse", "HEAD")
    assert job["workflow"]["source"] == (repo / ".whalehub/triage.md").read_text()
    assert job["workflow"]["model"] == "eu.anthropic.claude-haiku-5-5"
    assert set(job["event"]["issue"]) == {"number", "title", "body", "labels", "comments"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", job["created_at"])
    assert job["event"]["issue"]["labels"] == []
    assert module.read_json(path / "status.json")["status"] == "running"
    (repo / ".whalehub/triage.md").write_text(WORKFLOW)
    assert module.read_json(path / "job.json") == job
    response = client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 2})
    assert response.status_code == 409
    assert "active" in response.json["error"]
    assert len(list(config["RUNS_PATH"].iterdir())) == 1


def test_simultaneous_requests_publish_only_one_job(exercise):
    app, _, _, config = exercise
    app.config["EXECUTION_ENABLED"] = True
    def request_run(_):
        with app.test_client() as client:
            return client.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(request_run, range(2))) == [202, 409]
    assert len(list(config["RUNS_PATH"].iterdir())) == 1


def test_coding_workflow_has_no_label_gate(exercise):
    app, client, _, _ = exercise
    app.config["EXECUTION_ENABLED"] = True
    response = client.post("/api/runs", json={"workflow_path": ".whalehub/coding.md", "issue_id": 2})
    assert response.status_code == 202


def test_atomic_worker_claim_during_read(exercise, monkeypatch):
    _, client, _, _ = exercise
    path = publish(exercise)
    original = module.read_json
    def racing_read(file):
        if file == path / "job.json" and file.exists():
            file.rename(path / "claimed.json")
        return original(file)
    monkeypatch.setattr(module, "read_json", racing_read)
    response = client.get("/api/runs/" + path.name)
    assert response.status_code == 200
    assert response.json["status"] == "running"


def test_label_tool_updates_immediately_without_a_summary(exercise):
    _, client, _, config = exercise
    path = publish(exercise)
    response = set_labels(client, path, ["maintenance", "agent-ready"])
    assert response.status_code == 200
    assert response.json == {"issue_number": 1, "labels": ["maintenance", "agent-ready"]}
    assert module.read_json(path / "status.json")["status"] == "running"
    assert not (path / "result.json").exists()
    assert client.get("/api/issues").json["issues"][0]["labels"] == response.json["labels"]
    assert all(set(issue) == {"id", "title", "body", "labels", "comments"}
               for issue in module.read_json(config["ISSUES_PATH"]))


def test_reading_old_results_never_reapplies_labels(exercise):
    _, client, _, _ = exercise
    old = publish(exercise)
    assert set_labels(client, old, TRIAGE["labels"]).status_code == 200
    finish(old)
    new = publish(exercise)
    assert set_labels(client, new, ["question", "needs-human"]).status_code == 200
    finish(new, {"summary": "Newer run."})
    for path in (old, new):
        client.get("/api/runs/" + path.name)
    client.get("/api/runs")
    assert client.get("/api/issues").json["issues"][0]["labels"] == ["question", "needs-human"]
    assert client.post("/api/runs/" + old.name + "/labels", json={"issue_number": 1, "labels": []}).status_code == 409


@pytest.mark.parametrize("labels", [None, "bug", ["duplicate", "duplicate"], [" "], [["nested"]], [1]])
def test_invalid_label_tool_arguments_leave_issues_unchanged(exercise, labels):
    _, client, _, _ = exercise
    path = publish(exercise)
    response = set_labels(client, path, labels)
    assert response.status_code == 400
    assert "distinct non-empty strings" in response.json["error"]
    assert client.get("/api/issues").json["issues"][0]["labels"] == []


@pytest.mark.parametrize("labels", [["my-custom-label"], [], ["agent-ready", "needs-human"]])
def test_label_rules_belong_to_workflow_not_application(exercise, labels):
    _, client, _, _ = exercise
    path = publish(exercise)
    assert set_labels(client, path, labels).status_code == 200
    assert client.get("/api/issues").json["issues"][0]["labels"] == labels


@pytest.mark.parametrize("result", [{"summary": "I recommend maintenance and agent-ready."}, TRIAGE])
def test_final_response_never_updates_issue_fields(exercise, result):
    _, client, _, _ = exercise
    before = client.get("/api/issues").json
    path = publish(exercise)
    finish(path, result)
    assert client.get("/api/runs/" + path.name).json["status"] == "succeeded"
    assert client.get("/api/issues").json == before


def test_workflow_without_label_tool_cannot_change_labels(exercise):
    _, client, repo, _ = exercise
    (repo / ".whalehub/triage.md").write_text(WORKFLOW.replace("[SetIssueLabels]", "[]"))
    path = publish(exercise)
    response = set_labels(client, path, ["agent-ready"])
    assert response.status_code == 403
    assert "does not enable SetIssueLabels" in response.json["error"]
    assert client.get("/api/issues").json["issues"][0]["labels"] == []


def test_label_tool_is_bound_to_its_claimed_active_run(exercise):
    _, client, _, _ = exercise
    path = publish(exercise)
    url = "/api/runs/" + path.name + "/labels"
    assert client.post(url, json={"issue_number": 1, "labels": []}).status_code == 409
    set_labels(client, path, ["initial"])
    assert client.post(url, json={"issue_number": 1, "labels": [], "issue_id": 2}).status_code == 400
    assert client.post("/api/runs/" + "a" * 32 + "/labels", json={"issue_number": 1, "labels": []}).status_code == 404
    finish(path, status="failed")
    assert client.post(url, json={"issue_number": 1, "labels": []}).status_code == 409
    assert client.get("/api/issues").json["issues"][0]["labels"] == ["initial"]
    assert client.get("/api/issues").json["issues"][1]["labels"] == []


def test_successful_label_tool_changes_survive_later_run_failure(exercise):
    _, client, _, _ = exercise
    path = publish(exercise)
    assert set_labels(client, path, TRIAGE["labels"]).status_code == 200
    finish(path, status="failed")
    assert client.get("/api/runs/" + path.name).json["status"] == "failed"
    assert client.get("/api/issues").json["issues"][0]["labels"] == TRIAGE["labels"]


def test_restart_removes_legacy_explanation_without_resetting_labels(exercise):
    _, _, _, config = exercise
    path = config["STATE_PATH"] / "issues.json"
    issues = module.read_json(config["ISSUES_PATH"])
    issues[0].pop("comments")
    issues[0].update(labels=["maintenance"], explanation="Old model narration", labels_order=["old", "run"])
    module.write_json(path, issues)
    restarted = module.create_app({**config, "EXECUTION_ENABLED": True}).test_client()
    cleaned = restarted.get("/api/issues").json["issues"]
    assert cleaned[0] == {**{key: issues[0][key] for key in ("id", "title", "body", "labels")}, "comments": []}
    assert module.read_json(config["ISSUES_PATH"]) == cleaned
    assert not path.exists()
    assert (config["STATE_PATH"] / "issues.migrated.json").exists()
    response = restarted.post("/api/runs", json={"workflow_path": ".whalehub/triage.md", "issue_id": 1})
    job = module.read_json(config["RUNS_PATH"] / response.json["run_id"] / "job.json")
    assert set(job["event"]["issue"]) == {"number", "title", "body", "labels", "comments"}
    assert job["event"]["issue"]["labels"] == ["maintenance"]


def test_logs_results_and_plain_patch_survive_restart(exercise):
    _, client, _, config = exercise
    triage = publish(exercise)
    assert set_labels(client, triage, TRIAGE["labels"]).status_code == 200
    finish(triage)
    client.get("/api/runs/" + triage.name)
    path = publish(exercise, ".whalehub/coding.md")
    job = module.read_json(path / "job.json")
    assert set(job["event"]["issue"]) == {"number", "title", "body", "labels", "comments"}
    patch = "diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-old\n+new\n"
    log = "Reading files…\n" * 2000 + "Tests passed.\n"
    (path / "run.log").write_text(log)
    (path / "changes.patch").write_text(patch)
    (path / "uncommitted.patch").write_text(patch + "# Unfinished work retained.\n")
    result = {"summary": "Fixture patch", "base_commit": job["base_commit"], "result_commit": job["base_commit"],
              "test_exit_code": 0, "uncommitted_changes": True}
    finish(path, result, status="failed")
    restarted = module.create_app(config).test_client()
    data = restarted.get("/api/runs/" + path.name).json
    assert data["log"] == log and data["patch"] == patch and data["result"] == result
    assert data["uncommitted_patch"] == patch + "# Unfinished work retained.\n"
    assert restarted.get("/api/runs").json["runs"][0]["run_id"] == path.name
    assert restarted.get("/api/runs/not-a-run").status_code == 404


def test_create_workflow_commits_only_new_file_and_refuses_collisions(exercise):
    _, client, repo, _ = exercise
    source = repo / 'README.md'
    source.write_text(source.read_text() + '\nStaged user work.\n')
    git(repo, 'add', 'README.md')
    staged = git(repo, 'diff', '--cached')
    response = client.post('/api/workflows', json={'path': '.whalehub/my-review.md'})
    assert response.status_code == 201
    path = repo / response.json['path']
    definition = module.parse_workflow(path.read_text())
    assert set(definition) == {'name', 'model', 'trigger', 'tools', 'instructions'}
    assert git(repo, 'show', '--format=', '--name-only', 'HEAD') == '.whalehub/my-review.md'
    assert git(repo, 'diff', '--cached') == staged
    assert client.post('/api/workflows', json={'path': '.whalehub/my-review.md'}).status_code == 409
    assert client.post('/api/workflows', json={'path': '../outside.md'}).status_code == 404
    (repo / '.whalehub/link.md').symlink_to(repo / 'README.md')
    assert client.post('/api/workflows', json={'path': '.whalehub/link.md'}).status_code == 404


@pytest.mark.parametrize('selected', [None, 1, 2])
def test_optional_issue_event_and_no_output_field(exercise, selected):
    _, client, repo, _ = exercise
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('supplied issue.', 'issue #{{ whalehub.event.issue.number }}: {{ whalehub.event.issue.title }}.'))
    path = publish(exercise, issue=selected)
    job = module.read_json(path / 'job.json')
    assert 'output' not in job['workflow'] and 'issue' not in job
    assert job['event'].get('issue', {}).get('number') == selected
    assert client.get('/api/runs/' + path.name).json['issue_id'] == selected


def test_issue_tools_work_without_event_and_target_created_or_other_issues(exercise):
    _, client, repo, _ = exercise
    source = WORKFLOW.replace('[SetIssueLabels]', '[ReadIssues, CreateIssue, SetIssueLabels, AddIssueComment]')
    (repo / '.whalehub/triage.md').write_text(source)
    path = publish(exercise, issue=None)
    (path / 'job.json').rename(path / 'claimed.json')
    prefix = '/api/runs/' + path.name
    before = client.get(prefix + '/issues')
    assert before.status_code == 200 and len(before.json['issues']) == 2
    created = client.post(prefix + '/issues', json={'title': 'New task', 'body': 'Created by an agent.'})
    assert created.status_code == 201 and created.json['number'] == 3
    assert client.post(prefix + '/labels', json={'issue_number': 3, 'labels': ['follow-up']}).status_code == 200
    comment = client.post(prefix + '/comments', json={'issue_number': 3, 'body': 'Discussing the new task.'})
    assert comment.status_code == 201 and comment.json['issue_number'] == 3
    assert client.post(prefix + '/labels', json={'issue_number': 2, 'labels': ['custom']}).status_code == 200
    after = client.get(prefix + '/issues').json['issues']
    assert after[1]['labels'] == ['custom']
    assert after[2]['comments'][0]['run_id'] == path.name
    assert module.read_json(path / 'claimed.json')['event'] == {}
    finish(path, status='failed')
    assert client.get(prefix + '/issues').status_code == 409
    assert client.get('/api/issues').json['issues'][2]['labels'] == ['follow-up']


@pytest.mark.parametrize('selected,number', [(1, 1), (None, 2)])
def test_read_issue_returns_only_current_target_and_requires_active_run(exercise, selected, number):
    _, client, repo, _ = exercise
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('[SetIssueLabels]', '[ReadIssue]'))
    path = publish(exercise, issue=selected)
    snapshot = module.read_json(path / 'job.json')['event']
    prefix = '/api/runs/' + path.name
    assert client.get(prefix + f'/issues/{number}').status_code == 409
    (path / 'job.json').rename(path / 'claimed.json')
    assert client.put(f'/api/issues/{number}', json={'title': 'Updated title', 'body': 'Current contents.'}).status_code == 200
    comment = client.post(f'/api/issues/{number}/comments', json={'body': 'New discussion.'}).json
    response = client.get(prefix + f'/issues/{number}')
    assert response.status_code == 200
    assert response.json == {'number': number, 'title': 'Updated title', 'body': 'Current contents.',
                             'labels': [], 'comments': [comment]}
    assert module.read_json(path / 'claimed.json')['event'] == snapshot
    assert client.get(prefix + '/issues').status_code == 403
    assert client.get(prefix + '/issues/999').status_code == 404
    finish(path)
    assert client.get(prefix + f'/issues/{number}').status_code == 409


@pytest.mark.parametrize('tool,method,suffix,data', [
    ('ReadIssue', 'get', '/issues/1', None),
    ('ReadIssues', 'get', '/issues', None),
    ('CreateIssue', 'post', '/issues', {'title': 'Task', 'body': 'Body'}),
])
def test_issue_read_create_tools_require_explicit_configuration(exercise, tool, method, suffix, data):
    _, client, _, _ = exercise
    path = publish(exercise, issue=None)
    (path / 'job.json').rename(path / 'claimed.json')
    response = getattr(client, method)('/api/runs/' + path.name + suffix, json=data)
    assert response.status_code == 403 and tool in response.json['error']


def test_issue_created_workflow_can_be_saved_discovered_and_tested_manually(exercise):
    _, client, _, config = exercise
    source = WORKFLOW.replace('trigger: manual', 'trigger: issue_created')
    assert save(client, source).status_code == 200
    workflows = client.get('/api/workflows').json['workflows']
    assert next(item for item in workflows if item['path'] == '.whalehub/triage.md')['trigger'] == 'issue_created'
    assert not list(config['RUNS_PATH'].iterdir())
    path = publish(exercise)
    assert module.read_json(path / 'job.json')['workflow']['trigger'] == 'issue_created'


def test_new_issue_triggers_each_valid_matching_workflow_once_with_snapshots(exercise):
    app, client, repo, config = exercise
    app.config['EXECUTION_ENABLED'] = True
    source = WORKFLOW.replace('trigger: manual', 'trigger: issue_created')
    (repo / '.whalehub/triage.md').write_text(source)
    other = source.replace('name: Inspect issue', 'name: Second workflow')
    (repo / '.whalehub/second.md').write_text(other)
    (repo / '.whalehub/invalid.md').write_text(source.replace('[SetIssueLabels]', '[Unsupported]'))
    (repo / '.whalehub/binary.md').write_bytes(b'\xff')
    (repo / 'README.md').write_text(source)
    (repo / '.whalehub/link.md').symlink_to(repo / 'README.md')
    response = client.post('/api/issues', json={'title': 'New task', 'body': 'Please investigate.'})
    assert response.status_code == 201 and response.json['id'] == 3
    runs = client.get('/api/runs').json['runs']
    assert len(runs) == 2
    assert {run['run_id']: run['name'] for run in response.json['triggered_runs']} == {
        run['run_id']: run['workflow']['name'] for run in runs}
    stored = client.get('/api/issues').json['issues'][-1]
    assert 'triggered_runs' not in stored
    assert stored == {key: value for key, value in response.json.items() if key != 'triggered_runs'}
    jobs = {run['run_id']: module.read_json(config['RUNS_PATH'] / run['run_id'] / 'job.json') for run in runs}
    assert {job['workflow']['path'] for job in jobs.values()} == {'.whalehub/triage.md', '.whalehub/second.md'}
    for run in runs:
        job = jobs[run['run_id']]
        assert run['issue_id'] == 3 and run['status'] == 'running'
        assert job['base_commit'] == git(repo, 'rev-parse', 'HEAD')
        assert job['workflow']['source'] in (source, other)
        assert job['workflow']['trigger'] == 'issue_created'
        assert job['event'] == {'issue': {'number': 3, 'title': 'New task', 'body': 'Please investigate.', 'labels': [], 'comments': []}}
    client.put('/api/issues/3', json={'title': 'Updated task', 'body': 'Different requirements.'})
    client.post('/api/issues/3/comments', json={'body': 'Additional context.'})
    (repo / '.whalehub/triage.md').write_text(WORKFLOW)
    restarted = module.create_app(config).test_client()
    assert len(restarted.get('/api/runs').json['runs']) == 2
    for identifier, job in jobs.items():
        assert module.read_json(config['RUNS_PATH'] / identifier / 'job.json') == job


def test_issue_creation_queues_workflows_while_another_run_is_active(exercise):
    _, client, repo, config = exercise
    active = publish(exercise)
    (active / 'job.json').rename(active / 'claimed.json')
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    created = client.post('/api/issues', json={'title': 'Next task', 'body': 'Run after the active job.'})
    assert created.status_code == 201
    runs = client.get('/api/runs').json['runs']
    assert len(runs) == 2
    queued = next(run for run in runs if run['run_id'] != active.name)
    assert created.json['triggered_runs'] == [{'run_id': queued['run_id'], 'name': 'Inspect issue'}]
    assert queued['issue_id'] == created.json['id'] and queued['message'] == 'Waiting for the worker.'
    assert (config['RUNS_PATH'] / queued['run_id'] / 'job.json').exists()
    assert client.post('/api/runs', json={'workflow_path': '.whalehub/triage.md'}).status_code == 409


@pytest.mark.parametrize('trigger', ['manual', 'issue_created'])
def test_agent_created_issue_does_not_trigger_runs_but_can_be_run_manually(exercise, trigger):
    _, client, repo, config = exercise
    source = WORKFLOW.replace('[SetIssueLabels]', '[CreateIssue]').replace('trigger: manual', 'trigger: ' + trigger)
    (repo / '.whalehub/triage.md').write_text(source)
    active = publish(exercise, issue=None)
    (active / 'job.json').rename(active / 'claimed.json')
    (repo / '.whalehub/on-create.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    response = client.post('/api/runs/' + active.name + '/issues', json={'title': 'Agent follow-up', 'body': 'New issue.'})
    assert response.status_code == 201 and response.json['number'] == 3
    runs = client.get('/api/runs').json['runs']
    assert [run['run_id'] for run in runs] == [active.name]
    assert client.get('/api/issues').json['issues'][-1]['title'] == 'Agent follow-up'
    assert len(list(config['RUNS_PATH'].iterdir())) == 1
    # The origin policy cannot be overridden with tool arguments.
    assert client.post('/api/runs/' + active.name + '/issues', json={
        'title': 'Another issue', 'body': 'Body', 'trigger_workflows': True}).status_code == 400
    finish(active)
    manual = client.post('/api/runs', json={'workflow_path': '.whalehub/on-create.md', 'issue_id': 3})
    assert manual.status_code == 202
    job = module.read_json(config['RUNS_PATH'] / manual.json['run_id'] / 'job.json')
    assert job['event']['issue'] == response.json


def test_simultaneous_issue_creations_publish_one_triggered_job_each(exercise):
    app, client, repo, config = exercise
    app.config['EXECUTION_ENABLED'] = True
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    def create(index):
        with app.test_client() as peer:
            response = peer.post('/api/issues', json={'title': f'Task {index}', 'body': 'New issue.'})
            assert response.status_code == 201
            return response.json['id']
    with ThreadPoolExecutor(max_workers=3) as pool:
        identifiers = sorted(pool.map(create, range(3)))
    runs = client.get('/api/runs').json['runs']
    assert identifiers == [3, 4, 5] and sorted(run['issue_id'] for run in runs) == identifiers
    assert len(list(config['RUNS_PATH'].iterdir())) == 3


def test_disabled_execution_and_invalid_issues_do_not_trigger_runs(exercise):
    app, client, repo, config = exercise
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    response = client.post('/api/issues', json={'title': 'Stored only', 'body': 'Execution is disabled.'})
    assert response.status_code == 201 and response.json['triggered_runs'] == []
    assert not client.get('/api/runs').json['runs']
    app.config['EXECUTION_ENABLED'] = True
    assert client.post('/api/issues', json={'title': '', 'body': 'Invalid issue.'}).status_code == 400
    assert not list(config['RUNS_PATH'].iterdir())


def test_issue_creation_reports_no_runs_when_all_workflows_are_manual(exercise):
    app, client, _, _ = exercise
    app.config['EXECUTION_ENABLED'] = True
    response = client.post('/api/issues', json={'title': 'Manual task', 'body': 'No automatic workflow.'})
    assert response.status_code == 201 and response.json['triggered_runs'] == []
    assert not client.get('/api/runs').json['runs']


def test_triggered_run_requires_main_before_persisting_a_new_issue(exercise):
    app, client, repo, _ = exercise
    app.config['EXECUTION_ENABLED'] = True
    (repo / '.whalehub/triage.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    git(repo, 'checkout', '-b', 'user-branch')
    before = client.get('/api/issues').json
    response = client.post('/api/issues', json={'title': 'New task', 'body': 'Needs a run.'})
    assert response.status_code == 409 and 'main' in response.json['error']
    assert client.get('/api/issues').json == before
    assert not client.get('/api/runs').json['runs']


TEST_WORKFLOW = WORKFLOW.replace('[SetIssueLabels]', '[ReadIssue, ReadIssues, CreateIssue, SetIssueLabels, AddIssueComment]')
DEMO_ISSUE = {'title': 'Fix a welcome message', 'body': 'Change Welcom to Welcome in the book club greeting.'}


def publish_test(exercise, source=TEST_WORKFLOW):
    app, client, _, config = exercise
    app.config['EXECUTION_ENABLED'] = True
    response = client.post('/api/workflow-tests', json={
        'workflow_path': '.whalehub/triage.md', 'source': source, 'issue': DEMO_ISSUE})
    assert response.status_code == 202, response.json
    return config['RUNS_PATH'] / response.json['run_id']


@pytest.mark.parametrize('trigger', ['manual', 'issue_created'])
def test_workflow_test_snapshots_unsaved_text_without_saving_or_committing(exercise, trigger):
    _, client, repo, _ = exercise
    path = repo / '.whalehub/triage.md'
    path.write_text('An unfinished, invalid workflow on disk.\n')
    before = (path.read_bytes(), git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain'))
    source = TEST_WORKFLOW.replace('trigger: manual', 'trigger: ' + trigger) + '\nUnsaved test instructions.\n'
    directory = publish_test(exercise, source)
    job = module.read_json(directory / 'job.json')
    assert job['test'] is True and job['workflow']['source'] == source
    assert job['workflow']['trigger'] == trigger
    assert job['event']['issue'] == {'number': 1, **DEMO_ISSUE, 'labels': [], 'comments': []}
    assert (path.read_bytes(), git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain')) == before
    assert client.get('/api/runs').json == {'runs': [], 'busy': True}
    assert client.get('/api/runs/' + directory.name).status_code == 404
    assert [test['run_id'] for test in client.get('/api/workflow-tests').json['tests']] == [directory.name]


def test_all_test_issue_tools_are_isolated_and_results_survive_failure_and_restart(exercise):
    _, client, repo, config = exercise
    original = config['ISSUES_PATH'].read_bytes()
    # Even issue-created workflows must not fire for test-tool creations.
    (repo / '.whalehub/automatic.md').write_text(WORKFLOW.replace('trigger: manual', 'trigger: issue_created'))
    directory = publish_test(exercise)
    url = '/api/runs/' + directory.name
    assert client.get(url + '/issues').status_code == 409
    (directory / 'job.json').rename(directory / 'claimed.json')
    snapshot = module.read_json(directory / 'claimed.json')
    assert client.get(url + '/issues').json['issues'] == [snapshot['event']['issue']]
    for method, suffix, data in [
        ('GET', '/issues/2', None),
        ('POST', '/labels', {'issue_number': 2, 'labels': ['forbidden']}),
        ('POST', '/comments', {'issue_number': 2, 'body': 'Must not reach the real issue.'}),
    ]:
        assert client.open(url + suffix, method=method, json=data).status_code == 404
    assert client.post(url + '/labels', json={'issue_number': 1, 'labels': ['demo']}).status_code == 200
    assert client.post(url + '/comments', json={'issue_number': 1, 'body': 'Demo reviewed.'}).status_code == 201
    assert client.get(url + '/issues/1').json['labels'] == ['demo']
    created = client.post(url + '/issues', json={'title': 'Test follow-up', 'body': 'Private to this test.'})
    assert created.status_code == 201 and created.json['number'] == 2
    assert client.post(url + '/labels', json={'issue_number': 2, 'labels': ['follow-up']}).status_code == 200
    assert len(list(config['RUNS_PATH'].iterdir())) == 1
    assert config['ISSUES_PATH'].read_bytes() == original
    finish(directory, status='failed')
    (directory / 'run.log').write_text('Test activity before failure.\n')
    (directory / 'changes.patch').write_text('Test patch.\n')
    detail = client.get('/api/workflow-tests/' + directory.name).json
    assert detail['status'] == 'failed' and detail['source'] == TEST_WORKFLOW
    assert detail['log'] == 'Test activity before failure.\n' and detail['patch'] == 'Test patch.\n'
    assert detail['issues'][0]['comments'][0]['body'] == 'Demo reviewed.'
    assert detail['issues'][1]['labels'] == ['follow-up']
    assert module.read_json(directory / 'claimed.json') == snapshot
    assert client.post(url + '/labels', json={'issue_number': 1, 'labels': []}).status_code == 409
    restarted = module.create_app(config).test_client()
    assert restarted.get('/api/workflow-tests/' + directory.name).json == detail
    assert restarted.get('/api/runs').json == {'runs': [], 'busy': False}
    assert config['ISSUES_PATH'].read_bytes() == original


def test_each_workflow_test_starts_with_fresh_issue_state(exercise):
    _, client, _, _ = exercise
    first = publish_test(exercise)
    set_labels(client, first, ['old-test'])
    client.post('/api/runs/' + first.name + '/comments', json={'issue_number': 1, 'body': 'Old comment.'})
    client.post('/api/runs/' + first.name + '/issues', json={'title': 'Old follow-up', 'body': 'Discard for next test.'})
    finish(first)
    second = publish_test(exercise)
    detail = client.get('/api/workflow-tests/' + second.name).json
    assert detail['issues'] == [{'number': 1, **DEMO_ISSUE, 'labels': [], 'comments': []}]
    assert client.get('/api/workflow-tests/' + first.name).json['issues'][0]['labels'] == ['old-test']
    assert [test['run_id'] for test in client.get('/api/workflow-tests').json['tests']] == [second.name, first.name]
    finish(second)
    created = client.post('/api/issues', json={'title': 'Real issue', 'body': 'Uses the real numbering.'})
    assert created.json['id'] == 3


def test_test_sync_keeps_latest_draft_and_all_readable_results(exercise):
    _, client, repo, config = exercise
    sync = config['STATE_PATH'] / 'test-sync'
    draft = sync / 'workflows/triage.md'
    results = sync / 'results.md'
    saved = (repo / '.whalehub/triage.md').read_text()

    first_source = TEST_WORKFLOW + '\nFirst draft.\n'
    first = publish_test(exercise, first_source)
    assert draft.read_text() == first_source
    assert set_labels(client, first, ['maintenance', 'agent-ready']).status_code == 200
    assert client.post('/api/runs/' + first.name + '/comments',
                       json={'issue_number': 1, 'body': 'Needs a small fix.\nTests should pass.'}).status_code == 201
    finish(first)
    assert client.get('/api/workflow-tests/' + first.name).json['status'] == 'succeeded'

    second_source = TEST_WORKFLOW + '\nSecond draft.\n'
    second = publish_test(exercise, second_source)
    assert draft.read_text() == second_source
    assert [path.name for path in (sync / 'workflows').iterdir()] == ['triage.md']
    assert (repo / '.whalehub/triage.md').read_text() == saved
    finish(second, status='failed')
    assert client.get('/api/workflow-tests/' + second.name).json['status'] == 'failed'

    report = results.read_text()
    assert report.count('## Test ') == 2
    assert 'Outcome: succeeded' in report and 'Outcome: failed' in report
    assert 'Labels: "maintenance", "agent-ready"' in report
    assert '> Needs a small fix.\n> Tests should pass.' in report
    assert 'Comments: none' in report
    assert first.name not in report and second.name not in report
    assert 'run_id' not in report and 'comment_id' not in report

    inode = results.stat().st_ino
    assert client.get('/api/workflow-tests').status_code == 200
    assert results.stat().st_ino == inode
    draft.unlink()
    results.unlink()
    module.create_app(config)
    assert draft.read_text() == second_source
    assert results.read_text() == report


@pytest.mark.parametrize('change, status', [
    ({'source': 'Invalid workflow'}, 400),
    ({'issue': {'title': '', 'body': 'Body'}}, 400),
    ({'issue': {'title': 'Title', 'body': 'Body', 'id': 1}}, 400),
    ({'issue': None}, 400),
    ({'workflow_path': '../escape.md'}, 404),
])
def test_invalid_workflow_tests_do_not_publish_or_change_issues(exercise, change, status):
    app, client, _, config = exercise
    app.config['EXECUTION_ENABLED'] = True
    before = config['ISSUES_PATH'].read_bytes()
    response = client.post('/api/workflow-tests', json={
        'workflow_path': '.whalehub/triage.md', 'source': TEST_WORKFLOW, 'issue': DEMO_ISSUE, **change})
    assert response.status_code == status
    assert not list(config['RUNS_PATH'].iterdir())
    assert not list((config['STATE_PATH'] / 'test-issues').iterdir())
    assert config['ISSUES_PATH'].read_bytes() == before


def test_test_execution_respects_disabled_execution_and_tool_selection(exercise):
    app, client, _, _ = exercise
    request = {'workflow_path': '.whalehub/triage.md', 'source': WORKFLOW, 'issue': DEMO_ISSUE}
    assert client.post('/api/workflow-tests', json=request).status_code == 503
    directory = publish_test(exercise, WORKFLOW)
    (directory / 'job.json').rename(directory / 'claimed.json')
    assert client.get('/api/runs/' + directory.name + '/issues').status_code == 403
    assert client.post('/api/runs/' + directory.name + '/issues', json=DEMO_ISSUE).status_code == 403
    assert set_labels(client, directory, ['allowed']).status_code == 200
    finish(directory)
    normal = publish(exercise)
    assert client.get('/api/workflow-tests/' + normal.name).status_code == 404


@pytest.mark.parametrize('first_test', [True, False])
def test_main_runs_and_tests_share_the_busy_rule(exercise, first_test):
    _, client, _, _ = exercise
    directory = publish_test(exercise) if first_test else publish(exercise)
    assert client.post('/api/workflow-tests', json={
        'workflow_path': '.whalehub/triage.md', 'source': TEST_WORKFLOW, 'issue': DEMO_ISSUE}).status_code == 409
    assert client.post('/api/runs', json={'workflow_path': '.whalehub/triage.md'}).status_code == 409
    finish(directory)
    assert client.post('/api/workflow-tests', json={
        'workflow_path': '.whalehub/triage.md', 'source': TEST_WORKFLOW, 'issue': DEMO_ISSUE}).status_code == 202


def activity_entries(config):
    return (config['STATE_PATH'] / 'activity.log').read_text().splitlines()


def test_activity_records_actions_without_polling_noise(exercise):
    _, client, _, config = exercise
    assert client.get('/').status_code == 200
    click = {'control': 'save', 'label': 'Save workflow', 'tab': '#workflows', 'target': '.whalehub/triage.md'}
    assert client.post('/api/activity', json=click).status_code == 204
    saved = save(client).json
    assert client.post('/api/workflows', json={'path': '.whalehub/new.md'}).status_code == 201
    created = client.post('/api/issues', json=DEMO_ISSUE).json
    assert client.put('/api/issues/' + str(created['id']), json={**DEMO_ISSUE, 'title': 'Updated title'}).status_code == 200
    assert client.post('/api/issues/' + str(created['id']) + '/comments', json={'body': 'A learner comment.'}).status_code == 201
    directory = publish(exercise)
    before = (config['STATE_PATH'] / 'activity.log').read_bytes()
    for url in ['/health', '/api/files', '/api/files?path=.whalehub/triage.md', '/api/workflows',
                '/api/issues', '/api/runs', '/api/runs/' + directory.name, '/api/workflow-tests']:
        assert client.get(url).status_code == 200
    assert (config['STATE_PATH'] / 'activity.log').read_bytes() == before
    entries = activity_entries(config)
    assert len(entries) == 8
    assert all(re.match(r'^\[\d{2}:\d{2}:\d{2}\] \S', entry) for entry in entries)
    assert entries[0].endswith('Opened Whalehub')
    assert entries[1].endswith('Clicked Save workflow (workflows, .whalehub/triage.md)')
    assert entries[2].endswith(f"Saved workflow .whalehub/triage.md (commit {saved['commit'][:8]})")
    assert 'Created workflow .whalehub/new.md' in entries[3]
    assert f"Created issue #{created['id']}: {DEMO_ISSUE['title']}" in entries[4]
    assert f"Updated issue #{created['id']}: Updated title" in entries[5]
    assert f"Commented on #{created['id']} (comment #1)" in entries[6]
    assert entries[-1].endswith(f'Queued run .whalehub/triage.md for #1 (run {directory.name})')


def test_test_activity_preserves_the_unsaved_snapshot_after_claim(exercise):
    _, client, repo, config = exercise
    saved = (repo / '.whalehub/triage.md').read_bytes()
    source = TEST_WORKFLOW + '\nTry these unsaved instructions.\n'
    directory = publish_test(exercise, source)
    snapshot = module.read_json(directory / 'run.json')
    assert snapshot == module.read_json(directory / 'job.json')
    (directory / 'job.json').rename(directory / 'claimed.json')
    assert snapshot['test'] is True and snapshot['workflow']['source'] == source
    assert snapshot['event']['issue']['title'] == DEMO_ISSUE['title']
    assert module.read_json(directory / 'run.json') == snapshot
    assert (repo / '.whalehub/triage.md').read_bytes() == saved
    assert client.get('/api/workflow-tests/' + directory.name).status_code == 200
    entry, = activity_entries(config)
    assert entry.endswith(f'Queued test .whalehub/triage.md for #1 (run {directory.name})')


@pytest.mark.parametrize('claimed', [False, True])
def test_startup_snapshots_existing_runs_without_logging_new_activity(exercise, claimed):
    _, _, _, config = exercise
    directory = publish(exercise)
    snapshot = module.read_json(directory / 'job.json')
    (directory / 'run.json').unlink()
    if claimed:
        (directory / 'job.json').rename(directory / 'claimed.json')
    before = (config['STATE_PATH'] / 'activity.log').read_bytes()
    module.create_app(config)
    assert module.read_json(directory / 'run.json') == snapshot
    assert (directory / ('claimed.json' if claimed else 'job.json')).exists()
    assert (config['STATE_PATH'] / 'activity.log').read_bytes() == before


def test_automatic_run_activity_follows_the_issue_creation(exercise):
    app, client, _, config = exercise
    app.config['EXECUTION_ENABLED'] = True
    assert save(client, WORKFLOW.replace('trigger: manual', 'trigger: issue_created')).status_code == 200
    created = client.post('/api/issues', json=DEMO_ISSUE).json
    issue, queued = activity_entries(config)[-2:]
    assert f"Created issue #{created['id']}" in issue
    assert queued.endswith(f"Queued run .whalehub/triage.md for #{created['id']} automatically "
                           f"(run {created['triggered_runs'][0]['run_id']})")


@pytest.mark.parametrize('test', [False, True])
def test_agent_activity_identifies_the_run_and_test_scope(exercise, test):
    _, client, _, config = exercise
    if test:
        directory = publish_test(exercise)
    else:
        assert save(client, TEST_WORKFLOW).status_code == 200
        directory = publish(exercise)
    assert set_labels(client, directory, ['maintenance']).status_code == 200
    assert client.post('/api/runs/' + directory.name + '/comments',
                       json={'issue_number': 1, 'body': 'Agent comment.'}).status_code == 201
    assert client.post('/api/runs/' + directory.name + '/issues', json=DEMO_ISSUE).status_code == 201
    entries = activity_entries(config)[-3:]
    assert 'Agent: Set labels on #1: maintenance' in entries[0]
    assert 'Agent: Commented on #1 (comment #1)' in entries[1]
    assert 'Agent: Created issue #' in entries[2]
    scope = f"({'test, ' if test else ''}run {directory.name})"
    assert all(entry.endswith(scope) for entry in entries)


@pytest.mark.parametrize('data', [
    {}, {'control': 'save', 'actor': 'agent'}, {'control': ['save']}, {'control': 'x' * 257},
])
def test_invalid_clicks_do_not_create_activity(exercise, data):
    _, client, _, config = exercise
    assert client.post('/api/activity', json=data).status_code == 400
    assert not (config['STATE_PATH'] / 'activity.log').exists()


def test_failed_save_is_logged_without_a_success_event(exercise):
    _, client, repo, config = exercise
    before = (repo / '.whalehub/triage.md').read_bytes()
    response = save(client, 'Invalid workflow')
    assert response.status_code == 400
    entry, = activity_entries(config)
    assert entry.endswith('Failed PUT /api/workflows (400): ' + response.json['error'])
    assert (repo / '.whalehub/triage.md').read_bytes() == before


def test_concurrent_clicks_append_complete_activity_lines(exercise):
    app, _, _, config = exercise

    def click(number):
        with app.test_client() as client:
            return client.post('/api/activity', json={'control': 'test-workflow', 'target': str(number)}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(click, range(24))) == [204] * 24
    entries = activity_entries(config)
    assert len(entries) == 24
    assert {entry.split('] ', 1)[1] for entry in entries} == {
        f'Clicked test-workflow ({number})' for number in range(24)}


def test_activity_keeps_multiline_text_in_one_log_entry(exercise):
    _, client, _, config = exercise
    assert client.post('/api/activity', json={
        'control': 'test-workflow', 'label': 'Test\nworkflow', 'target': 'one\r\ntwo',
    }).status_code == 204
    entry, = activity_entries(config)
    assert entry.endswith('Clicked Test workflow (one two)')


def test_existing_jsonl_activity_is_converted_once(exercise):
    _, _, _, config = exercise
    legacy = config['STATE_PATH'] / 'activity.jsonl'
    entries = [
        {'timestamp': '2026-10-22T15:19:45+00:00', 'actor': 'learner', 'action': 'app_opened'},
        {'timestamp': '2026-10-22T15:19:47+00:00', 'actor': 'learner', 'action': 'workflow_test_queued',
         'workflow_path': '.whalehub/triage.md', 'issue_id': 1, 'run_id': 'a' * 32},
    ]
    original = ''.join(json.dumps(entry) + '\n' for entry in entries)
    legacy.write_text(original)
    # The starter log already exists when Compose bind-mounts it.
    log = config['STATE_PATH'] / 'activity.log'
    log.touch()
    inode = log.stat().st_ino
    module.create_app(config)
    assert activity_entries(config) == [
        '[15:19:45] Opened Whalehub',
        '[15:19:47] Queued test .whalehub/triage.md for #1 (run ' + 'a' * 32 + ')',
    ]
    assert not legacy.exists()
    assert (config['STATE_PATH'] / 'activity.migrated.jsonl').read_text() == original
    assert log.stat().st_ino == inode
    before = log.read_bytes()
    module.create_app(config)
    assert log.read_bytes() == before
