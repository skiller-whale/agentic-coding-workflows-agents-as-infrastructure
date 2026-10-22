# Flask / runner contract

Flask serves the UI, persists issues and publishes jobs. A separate Python worker
invokes Claude and collects artifacts. Both services see repositories, worktrees
and runs at matching `/workspace/...` paths.

## Publication and recovery

Flask refuses manual run submission while any job is running or waiting.
Automatic issue-creation jobs can queue while the worker is busy. It writes
`status.json` and `job.json` into a hidden `.pending-*` directory, then atomically publishes the
run directory. IDs are 32 lowercase hexadecimal characters. The worker holds an
exclusive `worker.lock` and claims jobs by renaming `job.json` to `claimed.json`.
The snapshot is immutable throughout execution. JSON writes use temporary files
and atomic replacement; Flask serializes updates with a private `app.lock`.

On worker restart, unfinished claimed jobs become failed without replaying Claude
or tools. Logs, metadata and artifacts remain available. Unclaimed jobs can still
execute. A second worker cannot claim or recover another active worker's jobs.
The worker executes queued jobs one at a time. Waiting jobs retain the existing
`running` status with a "Waiting for the worker" message until claimed.

`trigger: issue_created` is checked on each successful user issue
creation. Flask reads each saved `.whalehub/*.md` definition and queues one run
per valid match with the new issue as `event.issue`. Invalid definitions are
skipped; manual workflows, issue edits, comments and startup do not trigger jobs.
Execution must be enabled. If matching workflows exist, Flask checks the required
`main` checkout before saving the issue. Each job captures its workflow source,
issue event and base commit at creation, independently of later edits.

Workflow-originated issue creation through `CreateIssue` saves and returns the
new issue without publishing jobs. The server chooses this policy from the
creation route; it is not an argument the model can override. This stops recursive
issue/workflow chains. Explicit manual runs on agent-created issues remain allowed.

A job looks like:

```json
{
  "run_id": "<generated ID>",
  "created_at": "<UTC timestamp>",
  "base_commit": "<main HEAD>",
  "workflow": {
    "path": ".whalehub/review.md",
    "source": "<exact YAML/Markdown>",
    "name": "Review issues",
    "model": "eu.anthropic.claude-haiku-5-5",
    "trigger": "manual",
    "tools": ["ReadIssues"],
    "max_turns": 8,
    "timeout_seconds": 120,
    "instructions": "<Markdown body>"
  },
  "event": {}
}
```

If an issue was selected, `event.issue` contains `number`, `title`, `body`, `labels`
and `comments`. Otherwise the event is empty. `{{ whalehub.event.issue.number }}`
and other issue-field references expand only in workflow instructions. Lists
expand as JSON and absent fields as empty text. Event content remains literal.
Existing `{{ issue.id }}` references and old job `issue` snapshots are understood
for compatibility.

## Execution and artifacts

Workflow `max_turns` and `timeout_seconds` are optional positive integers. Each
overrides the corresponding runner environment default independently, using the
workflow snapshot in the job. Absent fields retain compatibility with old jobs.
The worker validates overrides before preparing a worktree, passes the effective
turn limit to Claude and enforces the effective execution timeout. Queueing,
repository preparation and artifact collection are outside that timeout.

Every run gets a Git branch and worktree from `base_commit`, including read-only
and issue-free workflows. The worker passes the rendered instructions, event
snapshot and current branch to Claude. It selects the configured model and tool
set explicitly. The `Read`, `Edit`, `Bash` and `Subagents` groups expand to
native tool names for both catalog selection and non-interactive approval; see
the mapping in [worker.py](runner/worker.py). Issue tools are configured individually
through MCP. Settings-file loading and slash commands are disabled, and only the
explicit MCP config is loaded. Claude and subprocesses
are terminated on timeout or shutdown, including surviving process-group children.

There is no label gate, compulsory final text, automatic pytest execution or
requirement that the working tree be clean. The instructions own routing, tests,
commits and reports. Success means Claude returned a successful completion with
exit code zero and artifact collection succeeded. It does not independently
verify the task or tests. Missing completion events, CLI errors and timeouts fail.

`status.json` contains status (`running`, `succeeded`, `failed`) and a message,
plus model/requested model, pinned CLI version, start/finish times, limits,
agent exit code, branch, worktree and base commit when available.

`run.log` contains readable assistant text, tools, stderr and final output.
`claude.jsonl` retains the streamed JSON. The runner collects actual Git state,
without staging or committing:

```json
{
  "summary": "<final text, possibly empty>",
  "base_commit": "<starting commit>",
  "result_commit": "<actual final HEAD>",
  "uncommitted_changes": false
}
```

`changes.patch` compares base and final commits. If work remains uncommitted,
`uncommitted.patch` compares base and the working tree, including untracked files.
The UI shows that fuller patch with a note; empty patches are hidden. Available
artifacts are collected after agent failures too. Finished worktrees are retained.
Historical results with `test_exit_code` remain displayable, but new runs do not
produce independent test results.

## Issue tools

The stdio MCP server forwards tool requests to the existing Flask API using
`WHALEHUB_API_URL`. Each route requires an active, claimed run and the named tool
in its immutable configuration. Issue targets are explicit and independent of
the event. Tool errors leave issue data unchanged; completed updates persist
if the run subsequently fails. The runner does not reapply tool results later.

| Tool | API | Payload/result |
| --- | --- | --- |
| `ReadIssue` | `GET /api/runs/<run_id>/issues/<issue_number>` | Returns one issue's number, title, body, labels and comments. |
| `ReadIssues` | `GET /api/runs/<run_id>/issues` | Returns `issues`, with numbers, bodies, labels and comments. |
| `CreateIssue` | `POST /api/runs/<run_id>/issues` | Accepts `title`, `body`; returns the new issue and number (201). |
| `SetIssueLabels` | `POST /api/runs/<run_id>/labels` | Accepts `issue_number`, `labels`; replaces the issue's labels. |
| `AddIssueComment` | `POST /api/runs/<run_id>/comments` | Accepts `issue_number`, `body`; returns the created comment (201). |

Issue numbers must be positive integers. Labels are distinct non-empty strings;
`[]` clears them. Comments and issue title/body must contain text. Agent comments
include trusted workflow attribution and a run link. Only configured tools are
registered by MCP and approved for the CLI.

The canonical file is `whalehub/data/issues.json`, mounted only in Flask.
`WHALEHUB_ISSUES` can select another path. Issue records use `id`; event and tool
responses expose that public identifier as `number`. Existing private issue data
migrates once to the canonical file, dropping obsolete explanations while
preserving progress. The old file becomes `issues.migrated.json`.

User APIs remain `GET/POST /api/issues`, `PUT /api/issues/<id>` and
`POST /api/issues/<id>/comments`. Editing preserves labels and discussion.

## Workflow and run APIs

`POST /api/workflows` accepts `{"path": ".whalehub/my-workflow.md"}`, creates a
four-field scaffold and commits only the new file. Existing names return 409;
paths outside the workflow directory and symlinks are refused. `PUT /api/workflows`
validates and saves full source, with an optional stale-editor hash, and commits
only that workflow. The main checkout must remain on `main`.

`POST /api/runs` accepts a workflow path and an optional `issue_id` (or null),
snapshots the current saved definition and event, and returns 202. Labels never
block execution. Both trigger types can be tested manually; a second active run
returns 409. Execution can be disabled with `WHALEHUB_EXECUTION_ENABLED`; disabled
manual run creation returns 503 and issue creation does not queue automatic runs.

`GET /api/runs` lists persisted runs. `GET /api/runs/<id>` returns metadata, log,
result, patches and a nullable `issue_id`. Reading results has no update side
effects. The browser polls the list, selected detail and issues while any run is
active, including while preparing another run. It preserves selection, drafts and
disclosures, ignores stale detail responses and retries refresh failures.

### Workflow tests

`POST /api/workflow-tests` accepts `workflow_path`, the unsaved workflow `source`,
and an `issue` object containing only `title` and `body`. It validates and snapshots
that source without saving or committing the file. Both trigger types can be
tested this way. Execution must be enabled and the worker idle, including queued
jobs; tests and regular manual runs share the existing busy rule.

Test jobs use the same publication, worker, worktree and artifact contract, with
`"test": true` added to the immutable job. The demo event starts at issue number 1
with empty labels and comments. Flask stores mutable test issues in the private
state directory at `test-issues/<run_id>.json` before publishing the job. Every
issue-tool route chooses that store from the trusted job flag. Reads, labels,
comments and newly created issues remain private to this test; creations never
dispatch other workflows. Every subsequent test starts with fresh issue state.

`GET /api/workflow-tests` lists tests newest first. `GET /api/workflow-tests/<id>`
returns the usual run artifacts plus the exact tested `source` and the current
test `issues`, including changes retained after failure. The UI restores the
latest test for each workflow and polls it in Workflows, preserving editor and
demo-issue drafts. Test jobs are excluded from `GET /api/runs` and return 404 from
its regular detail route. The shared `GET /api/runs` response also includes `busy`
so manual-run controls and polling account for active tests without listing them.
The run-scoped issue-tool URLs stay the same for the existing MCP server.

The worker reads `WHALEHUB_REPO`, `WHALEHUB_RUNS`, `WHALEHUB_WORKTREES`,
`WHALEHUB_API_URL`, `WHALEHUB_TIMEOUT_SECONDS` (180), `WHALEHUB_MAX_TURNS` (12),
`WHALEHUB_CLAUDE_BIN` (`claude`) and `WHALEHUB_ATTENDANCE_FILE`
(`/app/sync/attendance_id`). The attendance file is read-only, read for each run,
and never written into job, status or result files. Empty credentials fail before
invoking Claude. `/health` describes configuration, not worker liveness.
