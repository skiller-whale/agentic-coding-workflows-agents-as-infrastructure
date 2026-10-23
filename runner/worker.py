"""One serialized worker for Whalehub's file-based workflow jobs."""

import argparse
import codecs
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

from issue_tools import MCP_TOOLS


CLAUDE_VERSION = "2.1.293"
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
TOOL_GROUPS = {
    "Read": ("Read", "Glob", "Grep"),
    "Edit": ("Edit", "Write", "NotebookEdit"),
    "Bash": ("Bash",),
    "Subagents": ("Agent",),
}
TOOLS = TOOL_GROUPS.keys() | MCP_TOOLS.keys()


def render_instructions(instructions, event):
    """Expand event fields without evaluating expressions or templating issue text."""
    def substitute(match):
        expression = match.group(1).strip()
        if expression == "issue.id":  # Existing workflow files remain readable.
            expression = "whalehub.event.issue.number"
        field = expression.removeprefix("whalehub.event.issue.")
        if field == "id":
            field = "number"
        value = event.get("issue", {}).get(field, "")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return re.sub(r"\{\{(.*?)\}\}", substitute, instructions, flags=re.DOTALL)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Config:
    repo: Path = field(default_factory=lambda: Path(os.environ.get("WHALEHUB_REPO", "/workspace/ticket_parser")))
    runs: Path = field(default_factory=lambda: Path(os.environ.get("WHALEHUB_RUNS", "/workspace/runs")))
    worktrees: Path = field(default_factory=lambda: Path(os.environ.get("WHALEHUB_WORKTREES", "/workspace/worktrees")))
    attendance: Path = field(default_factory=lambda: Path(os.environ.get("WHALEHUB_ATTENDANCE_FILE", "/app/sync/attendance_id")))
    cli: str = field(default_factory=lambda: os.environ.get("WHALEHUB_CLAUDE_BIN", "claude"))
    api_url: str = field(default_factory=lambda: os.environ.get("WHALEHUB_API_URL", "http://whalehub:8000"))
    timeout: float = field(default_factory=lambda: float(os.environ.get("WHALEHUB_TIMEOUT_SECONDS", "180")))
    max_turns: int = field(default_factory=lambda: int(os.environ.get("WHALEHUB_MAX_TURNS", "12")))
    poll_seconds: float = 0.5

    def __post_init__(self):
        self.repo, self.runs, self.worktrees = (p.resolve() for p in (self.repo, self.runs, self.worktrees))
        if min(self.timeout, self.max_turns) <= 0:
            raise ValueError("Timeouts and max turns must be positive.")


@dataclass
class ProcessResult:
    exit_code: int
    failure: str = ""


class RunStopped(Exception):
    pass


class RunStop:
    def __init__(self, shutdown, directory):
        self.shutdown, self.marker = shutdown, directory / "stop.json"

    def is_set(self):
        return self.shutdown.is_set() or self.marker.exists()

    def message(self):
        return "Stopped by user." if self.marker.exists() else "Interrupted by worker shutdown."


def stop_group(process):
    """Also stop children that inherited stdout after their parent exited."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(command, cwd, env, timeout, stopped, consume):
    """Drain both pipes while enforcing a wall timeout and process-group cleanup."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    deadline, failure = time.monotonic() + timeout, ""
    with selectors.DefaultSelector() as selector:
        buffers = {"stdout": "", "stderr": ""}
        decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in buffers}
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map() or process.poll() is None:
                if not failure:
                    if stopped.is_set():
                        failure = stopped.message() if hasattr(stopped, "message") else "Interrupted by worker shutdown."
                    elif time.monotonic() >= deadline:
                        failure = f"Timed out after {timeout:g} seconds."
                    if failure:
                        stop_group(process)
                for key, _ in selector.select(0.05):
                    name = key.data
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    buffers[name] += decoders[name].decode(chunk, final=not chunk)
                    while "\n" in buffers[name]:
                        line, buffers[name] = buffers[name].split("\n", 1)
                        consume(name, line)
                    if not chunk:
                        if buffers[name]:
                            consume(name, buffers[name])
                        selector.unregister(key.fileobj)
                # Once the direct child finishes, no background tools should survive.
                if process.poll() is not None:
                    stop_group(process)
            return ProcessResult(process.wait(), failure)
        finally:
            stop_group(process)
            process.wait()
            process.stdout.close()
            process.stderr.close()


class ClaudeStream:
    def __init__(self, log, raw):
        self.log, self.raw = log, raw
        self.result, self.model = None, None
        self.partial_text = False

    def consume(self, channel, line):
        if channel == "stderr":
            self.log("[Claude stderr] " + line + "\n")
            return
        self.raw.write(line + "\n")
        self.raw.flush()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.log("[Claude stdout] " + line + "\n")
            return
        if not isinstance(event, dict):
            self.log("[Claude stdout] " + line + "\n")
            return
        kind = event.get("type")
        if kind == "system":
            self.model = event.get("model", self.model)
            self.log(f"[Claude {event.get('subtype', 'system')}] model={self.model or 'unknown'}\n")
        elif kind == "stream_event":
            inner = event.get("event", {})
            if not isinstance(inner, dict):
                return
            delta = inner.get("delta", {})
            if not isinstance(delta, dict):
                return
            if delta.get("type") == "text_delta":
                self.log(delta.get("text", ""))
                self.partial_text = True
            elif inner.get("type") == "content_block_stop" and self.partial_text:
                self.log("\n")
        elif kind in {"assistant", "user"}:
            message = event.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else message
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                content = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not self.partial_text:
                    self.log(block.get("text", "") + "\n")
                elif block.get("type") == "tool_use":
                    self.log(f"[Tool {block.get('name', '?')}] {json.dumps(block.get('input', {}), ensure_ascii=False)}\n")
                elif block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                    self.log(f"[Tool result{' error' if block.get('is_error') else ''}] {content}\n")
            if kind == "assistant":
                self.partial_text = False
        elif kind == "result":
            self.result = event
            self.log(f"[Claude result: {event.get('subtype', 'unknown')}]\n")
            if event.get("result"):
                self.log(str(event["result"]) + "\n")
            if event.get("structured_output") is not None:
                self.log(json.dumps(event["structured_output"], ensure_ascii=False) + "\n")
            for error in event.get("errors", []):
                self.log(str(error) + "\n")
        else:
            self.log(f"[Claude {kind or 'event'}] {json.dumps(event, ensure_ascii=False)}\n")


class Worker:
    def __init__(self, config, stopped=None):
        self.config, self.stopped = config, stopped or Event()

    def git(self, cwd, *arguments):
        result = subprocess.run(["git", "-c", f"safe.directory={self.config.repo}", "-c", f"safe.directory={cwd}",
                                 "-C", str(cwd), *arguments], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Git failed: " + result.stderr.strip())
        return result.stdout.rstrip("\n")

    def directories(self):
        return sorted(p for p in self.config.runs.iterdir() if RUN_ID.fullmatch(p.name) and p.is_dir())

    @contextmanager
    def run_locked(self, directory):
        with (directory / "control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def finish_status(self, directory, status):
        with self.run_locked(directory):
            if (directory / "stop.json").exists():
                status.update(status="stopped", message="Stopped by user.")
            write_json(directory / "status.json", status)
            return status

    def recover(self):
        for directory in self.directories():
            if not (directory / "claimed.json").exists():
                continue
            try:
                status = read_json(directory / "status.json")
            except (OSError, ValueError):
                status = {}
            if status.get("status") in {"succeeded", "failed", "stopped"}:
                continue
            message = ("Stopped by user." if (directory / "stop.json").exists() else
                       "Worker restarted after this job was claimed; it will not be replayed. Artifacts and worktree are retained.")
            with (directory / "run.log").open("a", encoding="utf-8") as log:
                log.write("[Runner] " + message + "\n")
            self.finish_status(directory, {**status, "status": "failed", "message": message, "finished_at": now()})

    def environment(self, cwd, model):
        attendance = self.config.attendance.read_text(encoding="utf-8").strip()
        if not attendance:
            raise ValueError("Attendance ID is empty; configure it before running a workflow.")
        env = os.environ.copy()
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"):
            env.pop(key, None)
        env.update(AWS_ACCESS_KEY_ID=attendance, AWS_SECRET_ACCESS_KEY="unused", AWS_CONFIG_FILE="/dev/null",
                   AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true", CLAUDE_CODE_USE_BEDROCK="1",
                   ANTHROPIC_MODEL=model, DISABLE_AUTOUPDATER="1")
        # Claude's Bash tools need the same explicit Git trust as the runner.
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        for path in (self.config.repo, cwd):
            env[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
            env[f"GIT_CONFIG_VALUE_{count}"] = str(path)
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env

    def execute(self, directory):
        config, job = self.config, read_json(directory / "claimed.json")
        stop = RunStop(self.stopped, directory)
        model = job.get("workflow", {}).get("model")
        metadata = {"model": model, "requested_model": model, "claude_version": CLAUDE_VERSION,
                    "started_at": now(), "timeout_seconds": config.timeout, "max_turns": config.max_turns}
        failures, result, checkout = [], None, None
        with (directory / "run.log").open("a", encoding="utf-8", buffering=1) as logfile:
            def log(text):
                logfile.write(text)
                logfile.flush()

            def status(message, state="running"):
                write_json(directory / "status.json", {**metadata, "status": state, "message": message})

            status("Preparing the workflow.")
            try:
                if stop.marker.exists():
                    raise RunStopped()
                if job.get("run_id") != directory.name:
                    raise ValueError("Job run ID does not match its directory.")
                workflow = job["workflow"]
                for name in ("max_turns", "timeout_seconds"):
                    if name in workflow:
                        value = workflow[name]
                        if type(value) is not int or value <= 0:
                            raise ValueError(f"{name} must be a positive integer when supplied.")
                        metadata[name] = value
                max_turns, timeout = metadata["max_turns"], metadata["timeout_seconds"]
                names = workflow["tools"]
                if (not isinstance(names, list) or any(not isinstance(n, str) or n not in TOOLS for n in names)
                        or len(set(names)) != len(names)):
                    raise ValueError("Job contains an invalid tool set.")
                if (not isinstance(model, str) or not model or any(c.isspace() for c in model)
                        or model.startswith("-")):
                    raise ValueError("The workflow must select a non-empty model ID without whitespace.")
                base = job["base_commit"]
                if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base):
                    raise ValueError("Job has an invalid base commit.")
                checkout = config.worktrees / directory.name
                metadata.update(branch=f"whalehub/run-{directory.name}", worktree=str(checkout), base_commit=base)
                self.git(config.repo, "worktree", "add", "-b", metadata["branch"], str(checkout), base)
                log(f"[Runner] Prepared {metadata['branch']} from {base}.\n")
                if stop.marker.exists():
                    raise RunStopped()
                env = self.environment(checkout, model)
                event = job.get("event")
                if event is None:
                    issue = job.get("issue")
                    event = {"issue": {**issue, "number": issue["id"]}} if issue else {}
                instructions = render_instructions(workflow["instructions"], event)
                prompt = ("Follow this workflow's instructions.\n\n" + instructions
                          + "\n\nWhalehub event (JSON):\n" + json.dumps(event, ensure_ascii=False)
                          + "\n\nYour current branch is " + metadata["branch"] + ".")
                builtins = [tool for name in names for tool in TOOL_GROUPS.get(name, ())]
                allowed = builtins + [MCP_TOOLS[name] for name in names if name in MCP_TOOLS]
                servers = {}
                if any(name in MCP_TOOLS for name in names):
                    servers["whalehub"] = {"type": "stdio", "command": sys.executable,
                                           "args": [str(Path(__file__).with_name("issue_tools.py")), str(directory)],
                                           "env": {"WHALEHUB_API_URL": config.api_url}}
                mcp_config = directory / "mcp.json"
                write_json(mcp_config, {"mcpServers": servers})
                # --bare hides search, file creation and delegation; --safe-mode also disables explicit MCP servers.
                command = [config.cli, "--print", prompt, "--setting-sources", "", "--disable-slash-commands",
                           "--output-format", "stream-json", "--verbose",
                           "--include-partial-messages", "--no-session-persistence", "--model", model,
                           "--tools", ",".join(builtins), "--max-turns", str(max_turns),
                           "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", str(mcp_config)]
                if allowed:
                    command += ["--allowedTools", ",".join(allowed)]
                if stop.marker.exists():
                    raise RunStopped()
                status("Claude is running.")
                log(f"[Runner] Claude Code {CLAUDE_VERSION}; model={model}; tools={names}; max turns={max_turns}; timeout={timeout:g}s.\n")
                with (directory / "claude.jsonl").open("a", encoding="utf-8") as raw:
                    stream = ClaudeStream(log, raw)
                    execution = run_process(command, checkout, env, timeout, stop, stream.consume)
                metadata["agent_exit_code"] = execution.exit_code
                if stream.model:
                    metadata["model"] = stream.model
                if execution.failure:
                    failures.append(execution.failure)
                if execution.exit_code:
                    failures.append(f"Claude exited with code {execution.exit_code}.")
                final = stream.result
                summary = final.get("result") if final else None
                result = {"summary": summary.strip() if isinstance(summary, str) else ""}
                if not final or final.get("subtype") != "success" or final.get("is_error") is not False:
                    failures.append("Claude did not complete successfully.")
            except RunStopped:
                failures.append("Stopped by user.")
            except Exception as error:
                failures.append(str(error))
            if checkout is not None and checkout.exists():
                try:
                    status("Saving run artifacts.")
                    result_commit = self.git(checkout, "rev-parse", "HEAD")
                    patch = self.git(checkout, "diff", "--binary", job["base_commit"], result_commit)
                    (directory / "changes.patch").write_text(patch + ("\n" if patch else ""), encoding="utf-8")
                    uncommitted = bool(self.git(checkout, "status", "--porcelain"))
                    if uncommitted:
                        unfinished = self.git(checkout, "diff", "--binary", job["base_commit"])
                        unfinished += "\n" if unfinished else ""
                        for name in self.git(checkout, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
                            if not name:
                                continue
                            new_file = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                                                      cwd=checkout, capture_output=True, text=True, timeout=30)
                            if new_file.returncode not in (0, 1):
                                raise RuntimeError("Could not capture an untracked file: " + new_file.stderr.strip())
                            unfinished += new_file.stdout
                        (directory / "uncommitted.patch").write_text(unfinished, encoding="utf-8")
                    result = {**(result or {"summary": ""}),
                              "base_commit": job["base_commit"], "result_commit": result_commit,
                              "uncommitted_changes": uncommitted}
                except Exception as error:
                    failures.append("Could not save coding artifacts: " + str(error))
            if result is not None:
                write_json(directory / "result.json", result)
            message = " ".join(failures) if failures else "Workflow completed successfully."
            metadata["finished_at"] = now()
            final_status = self.finish_status(directory, {**metadata, "status": "failed" if failures else "succeeded", "message": message})
            log("[Runner] " + final_status["message"] + "\n")

    def run(self, once=False):
        self.config.runs.mkdir(parents=True, exist_ok=True)
        self.config.worktrees.mkdir(parents=True, exist_ok=True)
        with (self.config.runs / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another worker already holds the runs directory lock.") from None
            self.recover()
            while not self.stopped.is_set():
                for directory in self.directories():
                    if self.stopped.is_set():
                        break
                    if (directory / "claimed.json").exists():
                        continue
                    with self.run_locked(directory):
                        try:
                            current_status = read_json(directory / "status.json").get("status")
                        except (OSError, ValueError):
                            current_status = None
                        if current_status in {"succeeded", "failed", "stopped"}:
                            continue
                        try:
                            (directory / "job.json").rename(directory / "claimed.json")
                        except FileNotFoundError:
                            continue
                    try:
                        self.execute(directory)
                    except Exception as error:
                        # A corrupt job cannot stop later jobs or be retried on restart.
                        self.finish_status(directory, {"status": "failed", "message": str(error), "finished_at": now()})
                if once:
                    break
                self.stopped.wait(self.config.poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Recover, drain current jobs, then exit.")
    args = parser.parse_args()
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    Worker(Config(), stopped).run(once=args.once)


if __name__ == "__main__":
    main()
