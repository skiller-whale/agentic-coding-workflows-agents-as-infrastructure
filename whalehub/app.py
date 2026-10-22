"""File-backed repository UI. Execution belongs to the separate runner."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4

from flask import Flask, jsonify, render_template, request
import yaml

TOOLS = {"Read", "Edit", "Bash", "Subagents", "SetIssueLabels", "AddIssueComment", "CreateIssue", "ReadIssue", "ReadIssues"}
GENERATED = {"__pycache__", "node_modules", "build", "dist", "venv"}
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
ISSUE_FIELDS = ("id", "title", "body", "labels", "comments")


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class WorkflowLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently replacing workflow settings."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise APIError("Workflow field names must be strings.")
        if key in result:
            raise APIError(f"Duplicate workflow field: {key}.")
        result[key] = loader.construct_object(value_node)
    return result


WorkflowLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_workflow(source):
    if not isinstance(source, str) or len(source.encode()) > 131072:
        raise APIError("Workflow source must be text of at most 128 KiB.")
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise APIError("Start the workflow with YAML frontmatter delimited by --- lines.")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise APIError("Close the YAML frontmatter with a --- line.")
    try:
        fields = yaml.load("".join(lines[1:end]), Loader=WorkflowLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" on line {mark.line + 2}" if mark else ""
        raise APIError(f"Invalid YAML{location}: {getattr(error, 'problem', str(error))}") from error
    if not isinstance(fields, dict):
        raise APIError("YAML frontmatter must contain name, model, trigger and tools.")
    required = {"name", "model", "trigger", "tools"}
    optional = {"max_turns", "timeout_seconds", "output"}
    if required - set(fields) or set(fields) - required - optional:
        detail = []
        if required - set(fields):
            detail.append("missing: " + ", ".join(sorted(required - set(fields))))
        if set(fields) - required - optional:
            detail.append("unsupported: " + ", ".join(sorted(set(fields) - required - optional)))
        raise APIError("Workflow fields — " + "; ".join(detail) + ".")
    if not isinstance(fields["name"], str) or not fields["name"].strip():
        raise APIError("name must be a non-empty string.")
    if (not isinstance(fields["model"], str) or not fields["model"]
            or any(c.isspace() for c in fields["model"]) or fields["model"].startswith("-")):
        raise APIError("model must be a non-empty model ID without whitespace.")
    if fields["trigger"] not in ("manual", "issue_created"):
        raise APIError("trigger must be manual or issue_created.")
    for field in ("max_turns", "timeout_seconds"):
        if field in fields and (type(fields[field]) is not int or fields[field] <= 0):
            raise APIError(f"{field} must be a positive integer when supplied.")
    # Accept the old field for existing workflows; it has no effect.
    if "output" in fields and fields.pop("output") not in ("summary", "patch"):
        raise APIError("Legacy output must be summary or patch; new workflows can omit it.")
    names = fields["tools"]
    if (not isinstance(names, list) or any(not isinstance(t, str) or t not in TOOLS for t in names)
            or len(set(names)) != len(names)):
        raise APIError("tools must be a list of distinct supported tools: " + ", ".join(sorted(TOOLS)) + ".")
    instructions = "".join(lines[end + 1:]).strip()
    if not instructions:
        raise APIError("Add non-empty Markdown instructions after the frontmatter.")
    for expression in re.findall(r"\{\{(.*?)\}\}", instructions, flags=re.DOTALL):
        if expression.strip() != "issue.id" and not re.fullmatch(
                r"whalehub\.event\.issue\.(number|id|title|body|labels|comments)", expression.strip()):
            raise APIError("Unknown workflow expression. Use {{ whalehub.event.issue.number }} or another issue field.")
    return {**fields, "instructions": instructions}


def atomic_text(path, text, preserve_metadata=False):
    original = path.stat() if preserve_metadata else None
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if original is not None:
                if (original.st_uid, original.st_gid) != (os.geteuid(), os.getegid()):
                    os.fchown(stream.fileno(), original.st_uid, original.st_gid)
                os.fchmod(stream.fileno(), original.st_mode & 0o777)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_text(path, json.dumps(value, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def activity_line(entry):
    action = entry["action"]
    path = entry.get("workflow_path", "")
    issue = f"#{entry.get('issue_id')}"
    commit = entry.get("commit", "")[:8]
    if action == "app_opened":
        message = "Opened Whalehub"
    elif action == "ui_clicked":
        context = ", ".join(value for value in (entry.get("tab", "").lstrip("#"), entry.get("target")) if value)
        message = "Clicked " + (entry.get("label") or entry["control"])
        if context:
            message += f" ({context})"
    elif action == "workflow_saved":
        message = f"Saved workflow {path} (commit {commit})" if entry["committed"] else f"Saved workflow {path} (unchanged)"
    elif action == "workflow_created":
        message = f"Created workflow {path} (commit {commit})"
    elif action in {"workflow_test_queued", "workflow_run_queued"}:
        kind = "test" if action == "workflow_test_queued" else "run"
        message = f"Queued {kind} {path}"
        if entry.get("issue_id") is not None:
            message += f" for {issue}"
        if entry.get("trigger") == "issue_created":
            message += " automatically"
        message += f" (run {entry['run_id']})"
    elif action in {"issue_created", "issue_updated"}:
        message = f"{'Created' if action == 'issue_created' else 'Updated'} issue {issue}: {entry['title']}"
    elif action == "issue_comment_added":
        message = f"Commented on {issue} (comment #{entry['comment_id']})"
    elif action == "issue_labels_updated":
        message = f"Set labels on {issue}: {', '.join(entry['labels'])}" if entry["labels"] else f"Cleared labels on {issue}"
    elif action == "request_failed":
        message = f"Failed {entry['method']} {entry['path']} ({entry['status']}): {entry['error']}"
    else:
        message = action.replace("_", " ").capitalize()
    if entry.get("actor") == "agent":
        message = f"Agent: {message} ({'test, ' if entry.get('test') else ''}run {entry['run_id']})"
    timestamp = datetime.fromisoformat(entry["timestamp"]).astimezone(timezone.utc).strftime("%H:%M:%S")
    return f"[{timestamp}] {' '.join(message.split())}\n"


def create_app(config=None):
    app = Flask(__name__, root_path=str(Path(__file__).resolve().parent))
    app.config.from_mapping(
        REPO_PATH=os.environ.get("WHALEHUB_REPO", "/workspace/ticket_parser"),
        RUNS_PATH=os.environ.get("WHALEHUB_RUNS", "/workspace/runs"),
        STATE_PATH=os.environ.get("WHALEHUB_STATE", "/var/lib/whalehub"),
        ISSUES_PATH=os.environ.get("WHALEHUB_ISSUES", str(Path(app.root_path) / "data/issues.json")),
        EXECUTION_ENABLED=os.environ.get("WHALEHUB_EXECUTION_ENABLED") == "1", MAX_CONTENT_LENGTH=150000,
    )
    app.config.update(config or {})
    repo = Path(app.config["REPO_PATH"]).resolve()
    runs = Path(app.config["RUNS_PATH"]).resolve()
    state = Path(app.config["STATE_PATH"]).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    issues_path = Path(app.config["ISSUES_PATH"]).resolve()
    issues_path.parent.mkdir(parents=True, exist_ok=True)
    test_issues = state / "test-issues"
    test_issues.mkdir(exist_ok=True)

    def append_activity(content):
        # Appending preserves the inode used by sync's read-only file mount.
        # A separate file lock also works while the caller holds app.lock.
        with (state / "activity.log").open("a", encoding="utf-8") as log:
            fcntl.flock(log, fcntl.LOCK_EX)
            log.write(content)

    def activity(action, *, actor="learner", **details):
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                 "actor": actor, "action": action, **details}
        try:
            append_activity(activity_line(entry))
        except OSError:
            app.logger.exception("Could not record activity")

    @contextmanager
    def locked():
        with (state / "app.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    with locked():
        legacy = state / "issues.json"
        source = legacy if legacy.exists() else issues_path
        if not source.exists():
            source = Path(app.root_path) / "data/issues.json"
        issues = read_json(source)
        # Keep existing issue data when upgrading the old private state file.
        cleaned = [{key: issue.get(key, []) if key == "comments" else issue[key]
                    for key in ISSUE_FIELDS} for issue in issues]
        for issue in cleaned:
            issue["comments"] = [
                {**comment, "author": "User"} if comment.get("author") == "Learner" and not comment.get("run_id") else comment
                for comment in issue["comments"]]
        if source != issues_path or cleaned != issues:
            write_json(issues_path, cleaned)
        if legacy.exists() and legacy != issues_path:
            legacy.rename(state / "issues.migrated.json")

    @app.errorhandler(APIError)
    def api_error(error):
        if request.method in {"POST", "PUT"} and request.path != "/api/activity":
            activity("request_failed", actor="app", method=request.method,
                     path=request.path, status=error.status, error=error.message)
        return jsonify(error=error.message), error.status

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="Request is too large (maximum 150 KB)."), 413

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise APIError("Send a JSON object.")
        return value

    def file_path(value, workflow=False, create=False):
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise APIError("Invalid repository file path.", 404)
        parts = value.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise APIError("Invalid repository file path.", 404)
        is_workflow = (len(parts) == 2 and parts[0] == ".whalehub" and parts[1].endswith(".md")
                       and not parts[1].startswith("."))
        ordinary = (value in ("README.md", "requirements.txt") or
                    (parts[0] in ("src", "tests", "samples") and len(parts) > 1 and
                     all(not p.startswith(".") and p not in GENERATED for p in parts[1:]) and
                     not value.endswith((".pyc", ".pyo"))))
        if not (is_workflow if workflow else (is_workflow or ordinary)):
            raise APIError("That file is outside the repository browser.", 404)
        path = repo
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise APIError("Symbolic links are not available in the repository browser.", 404)
        if not path.resolve().is_relative_to(repo) or (not create and not path.is_file()):
            raise APIError("Repository file not found.", 404)
        return path

    def source_for(path):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise APIError("Only UTF-8 text files can be viewed.", 415) from error

    def workflow_sources():
        for path in sorted((repo / ".whalehub").glob("*.md")):
            relative = path.relative_to(repo).as_posix()
            try:
                source = source_for(file_path(relative, workflow=True))
            except (APIError, FileNotFoundError):
                continue
            yield path, source

    def workflow_list():
        found = []
        for path, source in workflow_sources():
            relative = path.relative_to(repo).as_posix()
            item = {"path": relative, "name": path.stem, "valid": False}
            try:
                definition = parse_workflow(source)
                item.update(name=definition["name"], model=definition["model"], trigger=definition["trigger"], valid=True)
            except APIError as error:
                item["error"] = error.message
            found.append(item)
        return found

    def git(*arguments):
        result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True)
        if result.returncode:
            raise APIError("Git could not complete the operation: " + result.stderr.strip(), 409)
        return result.stdout.strip()

    def main_commit():
        if not (repo / ".git").is_dir() or git("rev-parse", "--show-toplevel") != str(repo):
            raise APIError("Run setup.sh to initialise the repository first.", 409)
        if git("branch", "--show-current") != "main":
            raise APIError("Workflow saves and runs require the repository checkout on main. Switch back to main when ready.", 409)
        return git("rev-parse", "HEAD")

    def run_dir(identifier):
        if not RUN_ID.fullmatch(identifier):
            raise APIError("Run not found.", 404)
        path = runs / identifier
        if path.is_symlink() or not path.is_dir():
            raise APIError("Run not found.", 404)
        return path

    def directories():
        return [p for p in runs.iterdir() if RUN_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]

    def job_for(path):
        try:
            return read_json(path / "job.json")
        except FileNotFoundError:
            # The worker's atomic claim may happen immediately before this read.
            return read_json(path / "claimed.json")

    def validate_labels(labels):
        if (not isinstance(labels, list) or any(not isinstance(v, str) or not v.strip() for v in labels)
                or len(set(labels)) != len(labels)):
            raise APIError("labels must be a list of distinct non-empty strings; [] clears the labels.")

    def issue_for(issues, identifier):
        issue = next((i for i in issues if i["id"] == identifier), None)
        if issue is None:
            raise APIError("Issue not found.", 404)
        return issue

    def agent_issue(issue):
        return {"number": issue["id"], **{key: issue[key] for key in ISSUE_FIELDS if key != "id"}}

    def event_for(job):
        if "event" in job:
            return job["event"]
        return {"issue": agent_issue(job["issue"])} if job.get("issue") else {}

    def issue_number(data):
        number = data.pop("issue_number", None)
        if type(number) is not int or number < 1:
            raise APIError("issue_number must be a positive integer.")
        return number

    def issue_store(path, job):
        return test_issues / (path.name + ".json") if job.get("test") else issues_path

    def require_idle():
        if any(read_json(path / "status.json")["status"] == "running" for path in directories()):
            raise APIError("Another run is active, including jobs waiting for the worker. Wait for it to finish.", 409)

    def publish_run(path, source, definition, issue, base, *, test=False, trigger="manual"):
        identifier = uuid4().hex
        job = {"run_id": identifier, "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
               "base_commit": base, "workflow": {"path": path.relative_to(repo).as_posix(), "source": source, **definition},
               "event": {"issue": agent_issue(issue)} if issue else {}}
        if test:
            job["test"] = True
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=runs))
        try:
            if test:
                write_json(test_issues / (identifier + ".json"), [issue])
            write_json(pending / "status.json", {"status": "running", "message": "Waiting for the worker."})
            # This immutable snapshot survives the worker's job -> claimed rename.
            write_json(pending / "run.json", job)
            write_json(pending / "job.json", job)
            pending.rename(runs / identifier)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
                if test:
                    (test_issues / (identifier + ".json")).unlink(missing_ok=True)
        activity("workflow_test_queued" if test else "workflow_run_queued",
                 actor="app" if trigger == "issue_created" else "learner",
                 run_id=identifier, workflow_path=job["workflow"]["path"],
                 workflow_name=definition["name"], issue_id=issue["id"] if issue else None,
                 trigger="test" if test else trigger)
        return {"run_id": identifier, "status": "running"}

    def issue_workflows():
        if not app.config["EXECUTION_ENABLED"]:
            return []
        matched = []
        for path, source in workflow_sources():
            try:
                definition = parse_workflow(source)
            except APIError:
                continue
            if definition["trigger"] == "issue_created":
                matched.append((path, source, definition))
        return matched

    def make_issue(fields, *, trigger_workflows, actor="learner", run_id=None):
        workflows = issue_workflows() if trigger_workflows else []
        base = main_commit() if workflows else None
        issues = read_json(issues_path)
        issue = {"id": max((i["id"] for i in issues), default=0) + 1,
                 **fields, "labels": [], "comments": []}
        issues.append(issue)
        write_json(issues_path, issues)
        activity("issue_created", actor=actor, issue_id=issue["id"], title=issue["title"], run_id=run_id, test=False)
        triggered = []
        for path, source, definition in workflows:
            run = publish_run(path, source, definition, issue, base, trigger="issue_created")
            triggered.append({"run_id": run["run_id"], "name": definition["name"]})
        return issue, triggered

    def text_fields(data, fields):
        if set(data) != set(fields):
            raise APIError("Send only these fields: " + ", ".join(fields) + ".")
        if any(not isinstance(data[key], str) or not data[key].strip() for key in fields):
            raise APIError("These fields must contain text: " + ", ".join(fields) + ".")
        return {key: data[key].strip() for key in fields}

    def tool_job(identifier, tool):
        path = run_dir(identifier)
        if not (path / "claimed.json").is_file() or read_json(path / "status.json")["status"] != "running":
            raise APIError("Issue tools require an active, claimed run.", 409)
        job = job_for(path)
        if tool not in job["workflow"]["tools"]:
            raise APIError(f"This workflow does not enable {tool}.", 403)
        return path, job

    def append_comment(issue, body, author, run_id=None):
        comment = {"id": max((c["id"] for c in issue["comments"]), default=0) + 1,
                   "body": body, "author": author,
                   "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        if run_id:
            comment["run_id"] = run_id
        issue["comments"].append(comment)
        return comment

    def inspect_run(path):
        job = job_for(path)
        status = read_json(path / "status.json")
        result, result_error = None, None
        if (path / "result.json").exists():
            try:
                result = read_json(path / "result.json")
            except (ValueError, UnicodeError):
                result_error = "Result is not valid UTF-8 JSON."
        return {
            "run_id": path.name, "created_at": job["created_at"],
            "workflow": {k: job["workflow"][k] for k in ("path", "name")},
            "issue_id": event_for(job).get("issue", {}).get("number"), "base_commit": job["base_commit"],
            **status, "log": (path / "run.log").read_text(encoding="utf-8", errors="replace") if (path / "run.log").exists() else "",
            "result": result, "result_error": result_error,
            "patch": (path / "changes.patch").read_text(encoding="utf-8", errors="replace") if (path / "changes.patch").exists() else None,
            "uncommitted_patch": (path / "uncommitted.patch").read_text(encoding="utf-8", errors="replace") if (path / "uncommitted.patch").exists() else None,
        }

    @app.get("/")
    def index():
        activity("app_opened")
        return render_template("index.html", execution_enabled=app.config["EXECUTION_ENABLED"])

    @app.post("/api/activity")
    def record_click():
        data = payload()
        if (set(data) - {"control", "label", "tab", "target"} or not data.get("control")
                or any(not isinstance(value, str) or len(value) > 256 for value in data.values())):
            raise APIError("Send a control and optional label, tab and target strings of at most 256 characters.")
        activity("ui_clicked", **data)
        return "", 204

    @app.get("/health")
    def health():
        enabled = app.config["EXECUTION_ENABLED"]
        return {"status": "ok", "stage": "runner-integration", "workflow_execution": enabled,
                "job_publication": enabled, "worker": "configured" if enabled else "disabled"}

    @app.get("/api/files")
    def files():
        if "path" in request.args:
            path = file_path(request.args["path"])
            source = source_for(path)
            return {"path": path.relative_to(repo).as_posix(), "source": source,
                    "sha256": hashlib.sha256(source.encode()).hexdigest()}
        found = []
        for root, dirs, names in os.walk(repo, followlinks=False):
            relative = Path(root).relative_to(repo)
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink() and
                       ((relative == Path(".") and d in ("src", "tests", "samples", ".whalehub")) or
                        (relative != Path(".") and relative.parts[0] != ".whalehub" and
                         not d.startswith(".") and d not in GENERATED))]
            for name in names:
                value = (relative / name).as_posix()
                try:
                    file_path(value)
                    found.append(value)
                except APIError:
                    pass
        return {"files": sorted(found)}

    @app.get("/api/workflows")
    def workflows():
        return {"workflows": workflow_list()}

    @app.put("/api/workflows")
    def save_workflow():
        data = payload()
        definition = parse_workflow(data.get("source"))
        with locked():
            main_commit()
            path = file_path(data.get("path"), workflow=True)
            before = source_for(path)
            if data.get("sha256") and data["sha256"] != hashlib.sha256(before.encode()).hexdigest():
                raise APIError("This workflow changed since you opened it. Reopen it before saving.", 409)
            relative = path.relative_to(repo).as_posix()
            if git("ls-files", "--unmerged", "--", relative):
                raise APIError("Resolve this workflow's Git conflict before saving.", 409)
            if before != data["source"]:
                atomic_text(path, data["source"], preserve_metadata=True)
            changed = bool(git("status", "--porcelain", "--", relative))
            if changed and not git("diff", "HEAD", "--", relative) and git("ls-files", "--", relative):
                changed = False
            if changed:
                git("add", "--", relative)
                # --only commits this working file and retains unrelated staged entries.
                git("commit", "--only", "-m", "Update workflow " + path.name, "--", relative)
            commit = git("rev-parse", "HEAD")
            activity("workflow_saved", workflow_path=relative, workflow_name=definition["name"],
                     commit=commit, committed=changed)
            return {"path": relative, "name": definition["name"], "committed": changed,
                    "commit": commit, "sha256": hashlib.sha256(data["source"].encode()).hexdigest()}

    @app.post("/api/workflows")
    def create_workflow():
        data = payload()
        if set(data) != {"path"}:
            raise APIError("Send only a path field.")
        if isinstance(data["path"], str) and len(data["path"].split("/")[-1].encode()) > 200:
            raise APIError("Choose a filename of at most 200 bytes.")
        with locked():
            main_commit()
            path = file_path(data["path"], workflow=True, create=True)
            if path.exists():
                raise APIError("A workflow with that filename already exists.", 409)
            fields = {"name": path.stem.replace("_", " ").replace("-", " ").capitalize(),
                      "model": "eu.anthropic.claude-haiku-5-5", "trigger": "manual", "tools": []}
            source = "---\n" + yaml.safe_dump(fields, sort_keys=False) + "---\n\nTODO: Write the workflow instructions.\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(path, source)
            relative = path.relative_to(repo).as_posix()
            git("add", "--", relative)
            git("commit", "--only", "-m", "Create workflow " + path.name, "--", relative)
            commit = git("rev-parse", "HEAD")
            activity("workflow_created", workflow_path=relative, commit=commit)
            return {"path": relative, "commit": commit}, 201

    @app.get("/api/issues")
    def issues():
        with locked():
            return {"issues": read_json(issues_path)}

    @app.post("/api/issues")
    def create_issue():
        fields = text_fields(payload(), ("title", "body"))
        with locked():
            issue, triggered = make_issue(fields, trigger_workflows=True)
            return {**issue, "triggered_runs": triggered}, 201

    @app.get("/api/runs/<identifier>/issues")
    def read_agent_issues(identifier):
        with locked():
            path, job = tool_job(identifier, "ReadIssues")
            return {"issues": [agent_issue(issue) for issue in read_json(issue_store(path, job))]}

    @app.get("/api/runs/<identifier>/issues/<int:number>")
    def read_agent_issue(identifier, number):
        with locked():
            path, job = tool_job(identifier, "ReadIssue")
            return agent_issue(issue_for(read_json(issue_store(path, job)), number))

    @app.post("/api/runs/<identifier>/issues")
    def create_agent_issue(identifier):
        fields = text_fields(payload(), ("title", "body"))
        with locked():
            path, job = tool_job(identifier, "CreateIssue")
            # Workflow-originated changes do not trigger more workflows.
            if job.get("test"):
                store = issue_store(path, job)
                issues = read_json(store)
                issue = {"id": max(i["id"] for i in issues) + 1, **fields, "labels": [], "comments": []}
                issues.append(issue)
                write_json(store, issues)
                activity("issue_created", actor="agent", issue_id=issue["id"], title=issue["title"],
                         run_id=identifier, test=True)
            else:
                issue, _ = make_issue(fields, trigger_workflows=False, actor="agent", run_id=identifier)
            return agent_issue(issue), 201

    @app.put("/api/issues/<int:identifier>")
    def edit_issue(identifier):
        fields = text_fields(payload(), ("title", "body"))
        with locked():
            issues = read_json(issues_path)
            issue = issue_for(issues, identifier)
            issue.update(fields)
            write_json(issues_path, issues)
            activity("issue_updated", issue_id=identifier, title=issue["title"])
            return issue

    @app.post("/api/issues/<int:identifier>/comments")
    def add_user_comment(identifier):
        fields = text_fields(payload(), ("body",))
        with locked():
            issues = read_json(issues_path)
            issue = issue_for(issues, identifier)
            comment = append_comment(issue, fields["body"], "User")
            write_json(issues_path, issues)
            activity("issue_comment_added", issue_id=identifier, comment_id=comment["id"])
            return comment, 201

    @app.post("/api/runs/<identifier>/comments")
    def add_agent_comment(identifier):
        data = payload()
        number = issue_number(data)
        fields = text_fields(data, ("body",))
        with locked():
            path, job = tool_job(identifier, "AddIssueComment")
            store = issue_store(path, job)
            issues = read_json(store)
            issue = issue_for(issues, number)
            comment = append_comment(issue, fields["body"], job["workflow"]["name"], identifier)
            write_json(store, issues)
            activity("issue_comment_added", actor="agent", issue_id=number, comment_id=comment["id"],
                     run_id=identifier, test=bool(job.get("test")))
            return {"issue_number": issue["id"], "comment": comment}, 201

    @app.post("/api/runs/<identifier>/labels")
    def set_issue_labels(identifier):
        data = payload()
        number = issue_number(data)
        if set(data) != {"labels"}:
            raise APIError("Send only a labels field.")
        validate_labels(data["labels"])
        with locked():
            path, job = tool_job(identifier, "SetIssueLabels")
            store = issue_store(path, job)
            issues = read_json(store)
            issue = issue_for(issues, number)
            issue["labels"] = data["labels"]
            write_json(store, issues)
            activity("issue_labels_updated", actor="agent", issue_id=number, labels=issue["labels"],
                     run_id=identifier, test=bool(job.get("test")))
            return {"issue_number": issue["id"], "labels": issue["labels"]}

    @app.get("/api/runs")
    def list_runs():
        with locked():
            paths = directories()
            values = [inspect_run(path) for path in paths if not job_for(path).get("test")]
            return {"runs": [{k: value[k] for k in ("run_id", "created_at", "workflow", "issue_id", "status", "message")}
                             for value in sorted(values, key=lambda v: (v["created_at"], v["run_id"]), reverse=True)],
                    "busy": any(read_json(path / "status.json")["status"] == "running" for path in paths)}

    @app.get("/api/runs/<identifier>")
    def get_run(identifier):
        with locked():
            path = run_dir(identifier)
            if job_for(path).get("test"):
                raise APIError("Run not found.", 404)
            return inspect_run(path)

    @app.get("/api/workflow-tests")
    def list_workflow_tests():
        with locked():
            values = [inspect_run(path) for path in directories() if job_for(path).get("test")]
            return {"tests": [{k: value[k] for k in ("run_id", "created_at", "workflow", "status", "message")}
                              for value in sorted(values, key=lambda v: (v["created_at"], v["run_id"]), reverse=True)]}

    @app.get("/api/workflow-tests/<identifier>")
    def get_workflow_test(identifier):
        with locked():
            path = run_dir(identifier)
            job = job_for(path)
            if not job.get("test"):
                raise APIError("Workflow test not found.", 404)
            return {**inspect_run(path), "source": job["workflow"]["source"],
                    "issues": [agent_issue(issue) for issue in read_json(issue_store(path, job))]}

    @app.post("/api/workflow-tests")
    def start_workflow_test():
        if not app.config["EXECUTION_ENABLED"]:
            raise APIError("Workflow execution is disabled for this app.", 503)
        data = payload()
        definition = parse_workflow(data.get("source"))
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise APIError("Supply a test issue with title and body.")
        fields = text_fields(issue, ("title", "body"))
        with locked():
            base = main_commit()
            path = file_path(data.get("workflow_path"), workflow=True)
            require_idle()
            return publish_run(path, data["source"], definition,
                               {"id": 1, **fields, "labels": [], "comments": []}, base, test=True), 202

    @app.post("/api/runs")
    def start_run():
        if not app.config["EXECUTION_ENABLED"]:
            raise APIError("Workflow execution is disabled for this app.", 503)
        data = payload()
        with locked():
            base = main_commit()
            path = file_path(data.get("workflow_path"), workflow=True)
            source = source_for(path)
            definition = parse_workflow(source)
            selected = data.get("issue_id")
            if selected is not None and type(selected) is not int:
                raise APIError("issue_id must be an issue number or null.")
            issue = issue_for(read_json(issues_path), selected) if selected is not None else None
            require_idle()
            return publish_run(path, source, definition, issue, base), 202

    # Preserve the sync snapshot for runs created before run.json was introduced.
    with locked():
        for path in directories():
            if not (path / "run.json").exists():
                try:
                    write_json(path / "run.json", job_for(path))
                except (OSError, ValueError):
                    app.logger.exception("Could not snapshot existing run %s", path.name)
        legacy_activity = state / "activity.jsonl"
        if legacy_activity.is_file():
            try:
                content = "".join(activity_line(json.loads(line)) for line in legacy_activity.read_text(encoding="utf-8").splitlines()
                                  if line.strip())
                append_activity(content)
                legacy_activity.rename(state / "activity.migrated.jsonl")
            except (OSError, ValueError, KeyError):
                app.logger.exception("Could not convert existing activity log")

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8000)
