import {editor} from './editor.bundle.js';
const $ = id => document.getElementById(id);
let fileEditor, codeEditor, patchEditor, opened, workflowItems = [], selectedRun, timer, runGeneration = 0, codeGeneration = 0, workflowGeneration = 0, busy = false;
const fileButtons = new Map();
const workflowButtons = new Map(), workflowDrafts = new Map();
let savingWorkflow = null, creatingWorkflow = false, workflowListGeneration = 0;
const demoIssue = {title: 'Fix a typo in the welcome message', body: 'The book club welcome message says "Welcom!". Change it to "Welcome!".'};
const workflowTests = new Map();
let startingTest = null, testFormPath = null, renderedTest = null, testPatchEditor, testIssuesSource = null, testListGeneration = 0;
const issueCards = new Map();
const issueButtons = new Map();
let selectedIssue = null, creatingIssue = false;
const runButtons = new Map();
let runItems = [], renderedRun = null, creatingRun = false, startingRun = false, pollingRuns = false, runsRefreshFailed = false, runRequest = 0, historyGeneration = 0;
const execution = document.body.dataset.execution === 'true';
function error(message = '') { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}
function button(label, action) {
  const value = document.createElement('button'); value.type = 'button'; value.textContent = label;
  value.addEventListener('click', () => action().catch(e => error(e.message))); return value;
}
function option(value, text) { const node = document.createElement('option'); node.value = value; node.textContent = text; return node; }
function runLink(identifier, text) {
  const link = document.createElement('a'); link.href = '#runs'; link.textContent = text;
  link.addEventListener('click', event => {
    event.preventDefault(); activateTab($('tab-runs'));
    showRun(identifier).catch(e => error(e.message));
  });
  return link;
}
const tabs = [...document.querySelectorAll('[role="tab"]')];
function showTab() {
  const active = tabs.find(tab => tab.hash === window.location.hash) || tabs[0];
  for (const tab of tabs) {
    const selected = tab === active;
    tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    $(tab.getAttribute('aria-controls')).hidden = !selected;
  }
}
window.addEventListener('hashchange', showTab);
function activateTab(tab) {
  if (window.location.hash !== tab.hash) window.history.pushState(null, '', tab.hash);
  showTab();
  if (tab.hash === '#issues') loadIssues().catch(e => error(e.message));
  if (tab.hash === '#workflows') Promise.all([loadWorkflows(), loadWorkflowTests()]).catch(e => error(e.message));
  if (tab.hash === '#runs') loadHistory().catch(e => error(e.message));
}
for (const tab of tabs) tab.addEventListener('click', event => { event.preventDefault(); activateTab(tab); });
document.querySelector('[role="tablist"]').addEventListener('keydown', event => {
  const current = tabs.indexOf(event.target);
  if (current < 0) return;
  let next;
  if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
  else if (event.key === 'ArrowLeft') next = (current + tabs.length - 1) % tabs.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = tabs.length - 1;
  else return;
  event.preventDefault(); activateTab(tabs[next]); tabs[next].focus();
});
function fileIcon(folder = false) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'file-icon' + (folder ? ' folder-icon' : ''));
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', folder ? 'M1.5 4h5L8 5.5h6.5v8h-13z M1.5 4V2.5h5L8 4' : 'M4 1.5h5l3 3v10H4z M9 1.5V5h3');
  svg.append(path); return svg;
}
function renderFiles(files) {
  const root = new Map(); fileButtons.clear();
  for (const path of files) {
    const parts = path.split('/'); let children = root;
    parts.forEach((name, index) => {
      if (!children.has(name)) children.set(name, {name, path: parts.slice(0, index + 1).join('/'), children: index < parts.length - 1 ? new Map() : null});
      children = children.get(name).children;
    });
  }
  function branch(children) {
    const list = document.createElement('ul');
    const items = [...children.values()].sort((a, b) => Number(Boolean(b.children)) - Number(Boolean(a.children)) || a.name.localeCompare(b.name));
    for (const item of items) {
      const row = document.createElement('li');
      const name = document.createElement('span'); name.textContent = item.name;
      if (item.children) {
        const folder = document.createElement('details'); folder.open = item.path === 'src';
        const summary = document.createElement('summary'); summary.append(fileIcon(true), name);
        folder.append(summary, branch(item.children)); row.append(folder);
      } else {
        const file = button('', () => openCodeFile(item.path)); file.className = 'file-link';
        file.setAttribute('aria-label', item.path); file.title = item.path; file.append(fileIcon(), name);
        fileButtons.set(item.path, file); row.append(file);
      }
      list.append(row);
    }
    return list;
  }
  $('file-tree').replaceChildren(branch(root));
}
async function openCodeFile(path) {
  const generation = ++codeGeneration;
  const file = await api('/api/files?path=' + encodeURIComponent(path));
  if (generation !== codeGeneration) return;
  codeEditor?.destroy(); codeEditor = editor($('code-editor'), file.source, {path});
  $('code-heading').textContent = 'ticket_parser / ' + path.split('/').join(' / ');
  for (const [filePath, button] of fileButtons) {
    if (filePath === path) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
  error();
}
function issueForm(fields, submitText, action) {
  const node = document.createElement('form'); node.className = 'issue-form';
  const inputs = {};
  for (const [name, caption] of Object.entries(fields)) {
    const label = document.createElement('label'); label.textContent = caption;
    const input = document.createElement(name === 'title' ? 'input' : 'textarea');
    input.name = name; input.required = true; inputs[name] = input;
    label.append(input); node.append(label);
  }
  const actions = document.createElement('div'); actions.className = 'form-actions';
  const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = submitText;
  actions.append(submit); node.append(actions);
  node.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true;
    const values = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
    try {
      await action(values);
      for (const [key, input] of Object.entries(inputs)) if (input.value === values[key]) input.value = '';
      error();
    } catch (e) { error(e.message); }
    finally { submit.disabled = false; }
  });
  return {node, inputs, actions};
}
function showIssueSelection() {
  $('issue-create').hidden = !creatingIssue;
  $('issue-placeholder').hidden = creatingIssue || issueCards.has(selectedIssue);
  for (const [id, record] of issueCards) record.card.hidden = creatingIssue || id !== selectedIssue;
  for (const [id, link] of issueButtons) {
    if (!creatingIssue && id === selectedIssue) link.setAttribute('aria-current', 'true');
    else link.removeAttribute('aria-current');
  }
}
function selectIssue(identifier) {
  selectedIssue = identifier; creatingIssue = false; showIssueSelection();
}
function createIssueCard(issue) {
  const card = document.createElement('article'); card.className = 'issue'; card.dataset.issueId = issue.id; card.hidden = true;
  const toolbar = document.createElement('div'); toolbar.className = 'toolbar';
  const title = document.createElement('h2');
  const automation = document.createElement('p'); automation.className = 'notice issue-automation'; automation.hidden = true;
  automation.setAttribute('role', 'status');
  const body = document.createElement('p'); body.className = 'issue-body';
  const labels = document.createElement('div'); labels.className = 'labels';
  const comments = document.createElement('div'); comments.className = 'comments';
  const record = {card, title, automation, body, labels, comments, issue, editing: false};
  function toggleEditing(active) {
    record.editing = active; editing.node.hidden = !active; edit.hidden = active; body.hidden = active;
    title.textContent = active ? `Edit issue #${issue.id}` : `#${issue.id} ${record.issue.title}`;
  }
  const editing = issueForm({title: 'Title', body: 'Issue description'}, 'Save issue', async values => {
    record.issue = await api(`/api/issues/${issue.id}`, {method: 'PUT', body: JSON.stringify(values)});
    toggleEditing(false); await loadIssues();
  });
  editing.node.hidden = true;
  editing.actions.append(button('Cancel', async () => toggleEditing(false)));
  const edit = button('Edit', async () => {
    editing.inputs.title.value = record.issue.title; editing.inputs.body.value = record.issue.body;
    toggleEditing(true); editing.inputs.title.focus();
  });
  const commenting = issueForm({body: 'Add a comment'}, 'Post comment', async values => {
    await api(`/api/issues/${issue.id}/comments`, {method: 'POST', body: JSON.stringify(values)});
    await loadIssues();
  });
  toolbar.append(title, edit);
  card.append(toolbar, automation, body, editing.node, labels, comments, commenting.node); $('issues').append(card);
  issueCards.set(issue.id, record); return record;
}
function renderComments(comments, container) {
  container.replaceChildren();
  for (const comment of comments) {
    const block = document.createElement('div'); block.className = 'comment';
    const meta = document.createElement('div'); meta.className = 'comment-meta';
    meta.textContent = `${comment.author}${comment.run_id ? ' · agent' : ''} · ${new Date(comment.created_at).toLocaleString()}`;
    if (comment.run_id) meta.append(runLink(comment.run_id, ' · View run'));
    const body = document.createElement('p'); body.className = 'comment-body'; body.textContent = comment.body;
    block.append(meta, body); container.append(block);
  }
}
async function loadIssues() {
  const {issues} = await api('/api/issues');
  const selected = $('run-issue').value;
  $('run-issue').replaceChildren(option('', 'No issue'), ...issues.map(issue => option(issue.id, `#${issue.id} ${issue.title}`)));
  $('run-issue').value = selected;
  for (const issue of issues) {
    let link = issueButtons.get(issue.id);
    if (!link) {
      link = button('', async () => selectIssue(issue.id)); link.className = 'issue-link';
      const number = document.createElement('span'); number.className = 'issue-number'; number.textContent = `#${issue.id}`;
      const name = document.createElement('span'); name.className = 'issue-name';
      link.append(number, name); $('issue-list').append(link); issueButtons.set(issue.id, link);
    }
    link.querySelector('.issue-name').textContent = issue.title; link.title = `#${issue.id} ${issue.title}`;
    const record = issueCards.get(issue.id) || createIssueCard(issue);
    record.issue = issue;
    const {title, body, labels, comments} = record;
    title.textContent = record.editing ? `Edit issue #${issue.id}` : `#${issue.id} ${issue.title}`;
    body.textContent = issue.body; labels.replaceChildren();
    for (const name of issue.labels) { const tag = document.createElement('span'); tag.className = 'label'; tag.textContent = name; labels.append(tag); }
    if (!issue.labels.length) { const empty = document.createElement('span'); empty.className = 'muted'; empty.textContent = 'Unlabelled'; labels.append(empty); }
    const serialized = JSON.stringify(issue.comments);
    if (record.commentsSource !== serialized) { renderComments(issue.comments, comments); record.commentsSource = serialized; }
  }
  if (selectedIssue === null && issues.length) selectedIssue = issues[0].id;
  showIssueSelection(); updateRunControls();
}
function updateWorkflowSelection() {
  for (const [path, record] of workflowButtons) {
    if (!creatingWorkflow && opened?.path === path) record.button.setAttribute('aria-current', 'true');
    else record.button.removeAttribute('aria-current');
    const dirty = workflowDrafts.get(path)?.dirty;
    record.state.hidden = !dirty && record.item.valid;
    record.state.textContent = dirty ? 'Unsaved' : 'Invalid'; record.state.dataset.status = dirty ? 'unsaved' : 'invalid';
  }
  $('workflow-create').hidden = !creatingWorkflow;
  $('workflow-detail').hidden = creatingWorkflow || !opened; $('workflow-placeholder').hidden = creatingWorkflow || Boolean(opened);
  if (!opened) return;
  const item = workflowItems.find(item => item.path === opened.path);
  $('editor-heading').textContent = item?.name || opened.path.split('/').pop();
  $('workflow-path').textContent = opened.path;
  $('workflow-error').hidden = !item?.error; $('workflow-error').textContent = item?.error || '';
  $('save').disabled = Boolean(savingWorkflow);
  $('discard-workflow').hidden = !opened.dirty; $('discard-workflow').disabled = Boolean(savingWorkflow);
  const message = opened.dirty ? 'Unsaved changes' : opened.status || '';
  $('save-status').textContent = message; $('save-status').hidden = !message;
  showWorkflowTest();
}
async function loadWorkflows() {
  const generation = ++workflowListGeneration;
  const {workflows} = await api('/api/workflows');
  if (generation !== workflowListGeneration) return;
  workflowItems = workflows;
  const selected = $('run-workflow').value;
  $('run-workflow').replaceChildren(...workflowItems.map(item => {
    const choice = option(item.path, item.name + (item.valid ? '' : ' (invalid)')); choice.disabled = !item.valid; return choice;
  }));
  $('run-workflow').value = workflowItems.find(item => item.path === selected && item.valid)?.path
    || workflowItems.find(item => item.valid)?.path || '';
  updateRunControls();
  const list = $('workflows');
  workflowItems.forEach((item, index) => {
    let record = workflowButtons.get(item.path);
    if (!record) {
      const link = button('', () => openWorkflow(item.path)); link.className = 'workflow-link'; link.dataset.workflowPath = item.path;
      const label = document.createElement('span'); label.className = 'workflow-label';
      const name = document.createElement('strong');
      const state = document.createElement('span'); state.className = 'workflow-state';
      const file = document.createElement('span'); file.className = 'workflow-file';
      label.append(name, state); link.append(label, file);
      record = {button: link, name, state, file}; workflowButtons.set(item.path, record);
    }
    record.item = item; record.name.textContent = item.name; record.file.textContent = item.path.split('/').pop();
    record.button.title = item.path;
    if (list.children[index] !== record.button) list.insertBefore(record.button, list.children[index] || null);
  });
  for (const [path, record] of workflowButtons) {
    if (!workflowItems.some(item => item.path === path)) { record.button.remove(); workflowButtons.delete(path); }
  }
  $('workflows-empty').hidden = workflowItems.length > 0;
  updateWorkflowSelection();
}
async function openWorkflow(path, discard = false) {
  if (!path) return;
  if (opened?.path === path && (opened.dirty || savingWorkflow === path) && !discard) return;
  const generation = ++workflowGeneration;
  const cached = workflowDrafts.get(path);
  const file = cached && (cached.dirty || savingWorkflow === path) && !discard ? cached
    : await api('/api/files?path=' + encodeURIComponent(path));
  if (generation !== workflowGeneration) return;
  if (opened) opened.draft = fileEditor.text();
  fileEditor?.destroy();
  const target = file === cached ? cached : {...file, draft: file.source, dirty: false};
  opened = target; creatingWorkflow = false; workflowDrafts.set(path, target);
  const instance = editor($('editor'), target.draft, {path, editable: true, onChange: () => {
    target.draft = instance.text(); target.dirty = target.draft !== target.source; updateWorkflowSelection();
  }});
  fileEditor = instance;
  updateWorkflowSelection(); error();
  const test = testFor(path);
  if (test.runId) await loadWorkflowTest(path, test.runId);
}
async function save() {
  if (!opened || savingWorkflow) return;
  const target = opened, source = fileEditor.text();
  savingWorkflow = target.path; updateWorkflowSelection();
  try {
    const value = await api('/api/workflows', {method: 'PUT', body: JSON.stringify({path: target.path, source, sha256: target.sha256})});
    target.source = source; target.sha256 = value.sha256;
    target.dirty = target.draft !== source;
    target.status = value.committed ? `Saved in commit ${value.commit.slice(0, 8)}` : 'Saved; no change to commit.';
    await loadWorkflows(); error();
  } finally { savingWorkflow = null; updateWorkflowSelection(); }
}
function resultText(run) {
  const result = run.result;
  const lines = [`Run ID: ${run.run_id}`, `Base commit: ${run.base_commit}`];
  if (run.model) lines.push(`Model: ${run.model}`);
  if (run.max_turns != null) lines.push(`Max turns: ${run.max_turns}`);
  if (run.timeout_seconds != null) lines.push(`Timeout: ${run.timeout_seconds} seconds`);
  if (run.branch) lines.push(`Branch: ${run.branch}`);
  if (result) {
    if (Array.isArray(result.labels)) {
      lines.push(`Labels: ${result.labels.join(', ') || '(cleared)'}`);
    }
    if (result.result_commit) lines.push(`Result commit: ${result.result_commit}`);
    if (Object.hasOwn(result, 'test_exit_code')) lines.push(`Verification tests: ${result.test_exit_code === 0 ? 'passed (exit 0)' : `exit ${result.test_exit_code ?? 'unavailable'}`}`);
  } else lines.push('No result yet.');
  return lines.join('\n\n');
}
function runState(node, status) {
  node.textContent = status[0].toUpperCase() + status.slice(1); node.dataset.status = status;
}
function testFor(path) {
  if (!workflowTests.has(path)) workflowTests.set(path, {issue: {...demoIssue}, runId: null, result: null, minimumHeight: 0, error: ''});
  return workflowTests.get(path);
}
function updateTestControls() {
  $('test-workflow').disabled = !execution || busy || startingRun || Boolean(startingTest) || !opened;
  const message = !execution ? 'Workflow execution is disabled.' : startingTest ? 'Starting test…'
    : busy ? (opened && testFor(opened.path).result?.status === 'running' ? 'Test in progress.' : 'Another run or test is in progress.') : '';
  $('workflow-test-availability').textContent = message; $('workflow-test-availability').hidden = !message;
}
function renderTestIssues(issues) {
  const source = JSON.stringify(issues);
  if (testIssuesSource === source) return;
  testIssuesSource = source;
  const cards = issues.map(issue => {
    const card = document.createElement('div'); card.className = 'test-issue';
    const heading = document.createElement('h5'); heading.textContent = `#${issue.number} ${issue.title}`;
    const body = document.createElement('p'); body.className = 'issue-body'; body.textContent = issue.body;
    const labels = document.createElement('div'); labels.className = 'labels';
    for (const label of issue.labels) { const node = document.createElement('span'); node.className = 'label'; node.textContent = label; labels.append(node); }
    if (!issue.labels.length) { const node = document.createElement('span'); node.className = 'muted'; node.textContent = 'No labels'; labels.append(node); }
    const comments = document.createElement('div'); comments.className = 'test-issue-comments';
    const commentsHeading = document.createElement('h6'); commentsHeading.textContent = 'Comments'; comments.append(commentsHeading);
    if (!issue.comments.length) { const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'No comments yet.'; comments.append(empty); }
    for (const comment of issue.comments) {
      const node = document.createElement('div'); node.className = 'comment';
      const author = document.createElement('p'); author.className = 'comment-meta'; author.textContent = `By ${comment.author}`;
      const text = document.createElement('p'); text.className = 'comment-body'; text.textContent = comment.body;
      node.append(author, text); comments.append(node);
    }
    card.append(heading, body, labels, comments); return card;
  });
  $('workflow-test-issues').replaceChildren(...cards);
}
function showWorkflowTest() {
  updateTestControls();
  if (!opened) return;
  const test = testFor(opened.path);
  if (testFormPath !== opened.path) {
    testFormPath = opened.path;
    $('workflow-test-title').value = test.issue.title; $('workflow-test-body').value = test.issue.body;
  }
  $('workflow-test-error').hidden = !test.error; $('workflow-test-error').textContent = test.error;
  const run = test.result;
  $('workflow-test-result').hidden = !run;
  $('workflow-test-result').style.setProperty('--test-result-min-height', `${test.minimumHeight}px`);
  if (!run) return;
  if (renderedTest !== run.run_id) {
    $('workflow-test-result').open = true;
    $('workflow-test-log-panel').open = run.status !== 'succeeded';
    $('workflow-test-patch-panel').open = true;
    renderedTest = run.run_id;
  }
  runState($('workflow-test-state'), run.status);
  $('workflow-test-created').dateTime = run.created_at;
  $('workflow-test-created').textContent = new Date(run.created_at).toLocaleString();
  $('workflow-test-status').textContent = run.message;
  $('workflow-test-draft').hidden = run.source === opened.draft;
  $('workflow-test-result-error').hidden = !run.result_error;
  $('workflow-test-result-error').textContent = run.result_error || '';
  const log = run.log || 'No log yet.';
  if ($('workflow-test-log').textContent !== log) $('workflow-test-log').textContent = log;
  $('workflow-test-source').textContent = run.source;
  $('workflow-test-metadata').textContent = resultText(run);
  $('workflow-test-unfinished').hidden = !run.result?.uncommitted_changes;
  renderTestIssues(run.issues);
  const patch = run.uncommitted_patch ?? run.patch;
  $('workflow-test-patch-panel').hidden = !patch;
  if (patch != null && testPatchEditor?.text() !== patch) {
    testPatchEditor?.destroy(); testPatchEditor = editor($('workflow-test-patch'), patch, {patch: true});
  }
}
async function loadWorkflowTest(path, identifier) {
  const test = testFor(path);
  const request = test.request = (test.request || 0) + 1;
  const run = await api('/api/workflow-tests/' + identifier);
  if (test.runId !== identifier || test.request !== request) return;
  if (opened?.path === path && test.result && test.result.run_id !== identifier) {
    const bounds = $('workflow-test-result').getBoundingClientRect();
    // Keep the visible area when replacing a longer result with a shorter one.
    test.minimumHeight = Math.min(bounds.height, Math.max(0, window.innerHeight - bounds.top));
  }
  test.result = run;
  if (opened?.path === path) showWorkflowTest();
}
async function loadWorkflowTests() {
  if (startingTest) return;
  const generation = ++testListGeneration;
  const {tests} = await api('/api/workflow-tests');
  if (generation !== testListGeneration || startingTest) return;
  if (tests.some(test => test.status === 'running')) {
    busy = true; updateRunControls(); scheduleRunRefresh();
  }
  const seen = new Set();
  for (const run of tests) {
    const path = run.workflow.path;
    if (seen.has(path)) continue;
    seen.add(path);
    const test = testFor(path);
    if (test.runId !== run.run_id) test.runId = run.run_id;
  }
  if (opened) {
    const test = testFor(opened.path);
    if (test.runId) await loadWorkflowTest(opened.path, test.runId);
    else showWorkflowTest();
  }
}
async function startWorkflowTest() {
  if ($('test-workflow').disabled || !opened) return;
  const path = opened.path, source = fileEditor.text(), test = testFor(path);
  startingTest = path; ++testListGeneration; test.error = ''; updateRunControls(); showWorkflowTest();
  try {
    const value = await api('/api/workflow-tests', {method: 'POST', body: JSON.stringify({workflow_path: path, source, issue: {...test.issue}})});
    test.runId = value.run_id;
    busy = true; updateRunControls(); scheduleRunRefresh();
    if (opened?.path === path) showWorkflowTest();
    await Promise.all([loadWorkflowTest(path, value.run_id), loadHistory()]);
  } catch (e) { test.error = e.message; }
  finally { startingTest = null; updateRunControls(); if (opened?.path === path) showWorkflowTest(); }
}
function updateRunControls() {
  const validWorkflow = workflowItems.some(item => item.path === $('run-workflow').value && item.valid);
  $('run').disabled = !execution || busy || startingRun || Boolean(startingTest) || !validWorkflow;
  $('run-workflow').disabled = startingRun; $('run-issue').disabled = startingRun;
  $('cancel-run').disabled = startingRun; $('new-run').disabled = startingRun;
  const message = !execution ? 'Workflow execution is disabled.' : startingRun ? 'Starting workflow…'
    : busy ? 'Another run is in progress.' : !validWorkflow ? 'Save a valid workflow before starting a run.' : '';
  $('run-availability').textContent = message; $('run-availability').hidden = !message;
  updateTestControls();
}
function showRunSelection() {
  $('run-create').hidden = !creatingRun;
  $('run-detail').hidden = creatingRun || renderedRun !== selectedRun || !selectedRun;
  $('run-placeholder').hidden = creatingRun || (selectedRun && renderedRun === selectedRun);
  $('run-placeholder').textContent = selectedRun ? 'Loading run…' : 'Choose a run or start a workflow.';
  for (const [id, record] of runButtons) {
    if (!creatingRun && id === selectedRun) record.button.setAttribute('aria-current', 'true');
    else record.button.removeAttribute('aria-current');
  }
}
function scheduleRunRefresh() {
  if (!busy && !runsRefreshFailed) { clearTimeout(timer); timer = null; }
  else if (!timer && !pollingRuns) timer = setTimeout(refreshRuns, 1000);
}
async function loadHistory() {
  const generation = ++historyGeneration;
  const {runs, busy: workerBusy} = await api('/api/runs');
  if (generation !== historyGeneration) return runItems;
  runItems = runs;
  const list = $('run-list');
  runs.forEach((run, index) => {
    let record = runButtons.get(run.run_id);
    if (!record) {
      const link = button('', () => showRun(run.run_id)); link.className = 'run-link'; link.dataset.runId = run.run_id;
      const label = document.createElement('span'); label.className = 'run-label';
      const name = document.createElement('strong');
      const state = document.createElement('span'); state.className = 'run-state';
      const info = document.createElement('span'); info.className = 'run-info';
      label.append(name, state); link.append(label, info);
      record = {button: link, name, state, info}; runButtons.set(run.run_id, record);
    }
    record.name.textContent = run.workflow.name; runState(record.state, run.status);
    record.info.textContent = `${run.issue_id == null ? 'Manual run' : `Issue #${run.issue_id}`} · ${new Date(run.created_at).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
    record.button.title = `${run.workflow.name} · ${run.issue_id == null ? 'no issue' : `issue #${run.issue_id}`}\n${new Date(run.created_at).toLocaleString()}\nRun ${run.run_id}`;
    if (list.children[index] !== record.button) list.insertBefore(record.button, list.children[index] || null);
  });
  for (const [id, record] of runButtons) if (!runs.some(run => run.run_id === id)) { record.button.remove(); runButtons.delete(id); }
  $('runs-empty').hidden = runs.length > 0;
  busy = workerBusy ?? runs.some(run => run.status === 'running');
  updateRunControls(); showRunSelection(); scheduleRunRefresh();
  return runs;
}
async function loadRun(identifier) {
  const generation = runGeneration, request = ++runRequest;
  const run = await api('/api/runs/' + identifier);
  if (generation !== runGeneration || identifier !== selectedRun || request !== runRequest) return;
  if (renderedRun !== identifier) {
    $('run-log-panel').open = run.status !== 'succeeded'; $('run-metadata').open = false; $('patch-panel').open = true;
  }
  renderedRun = identifier;
  $('run-heading').textContent = run.workflow.name; runState($('run-state'), run.status);
  $('run-issue-link').hidden = run.issue_id == null;
  $('run-issue-link').textContent = `Issue #${run.issue_id}`; $('run-issue-link').dataset.issueId = run.issue_id;
  $('run-created').dateTime = run.created_at; $('run-created').textContent = new Date(run.created_at).toLocaleString();
  $('run-status').textContent = run.message;
  const log = run.log || 'No log yet.';
  if ($('run-log').textContent !== log) $('run-log').textContent = log;
  $('run-outcome').hidden = !run.result?.summary; $('run-summary').textContent = run.result?.summary || '';
  $('run-result-error').hidden = !run.result_error; $('run-result-error').textContent = run.result_error || '';
  $('run-unfinished').hidden = !run.result?.uncommitted_changes;
  $('run-check').hidden = !run.result || !Object.hasOwn(run.result, 'test_exit_code');
  const exit = run.result?.test_exit_code;
  $('run-check').textContent = exit === 0 ? 'Verification tests passed.' : exit == null ? 'Verification tests unavailable.' : `Verification tests failed (exit ${exit}).`;
  $('run-check').dataset.status = exit === 0 ? 'succeeded' : 'failed';
  $('run-result').textContent = resultText(run);
  const patch = run.uncommitted_patch ?? run.patch;
  $('patch-panel').hidden = !patch;
  if (patch != null && patchEditor?.text() !== patch) {
    patchEditor?.destroy(); patchEditor = editor($('patch'), patch, {patch: true});
  }
  showRunSelection();
}
async function refreshRuns() {
  timer = null; pollingRuns = true;
  try {
    await loadHistory();
    await Promise.all([selectedRun && !creatingRun ? loadRun(selectedRun) : Promise.resolve(), loadIssues(), loadWorkflowTests()]);
    if (runsRefreshFailed) error();
    runsRefreshFailed = false;
  } catch (e) { runsRefreshFailed = true; error('Could not refresh runs: ' + e.message + '. Retrying…'); }
  finally { pollingRuns = false; scheduleRunRefresh(); }
}
async function showRun(identifier) {
  if (!identifier) return;
  if (selectedRun !== identifier) renderedRun = null;
  selectedRun = identifier; creatingRun = false; ++runGeneration; showRunSelection();
  await loadRun(identifier); error();
}
async function newRun() {
  creatingRun = true; showRunSelection();
  await Promise.all([loadHistory(), loadWorkflows(), loadIssues()]);
  if (creatingRun) $('run-workflow').focus();
}
async function startRun() {
  if ($('run').disabled) return;
  startingRun = true; updateRunControls();
  try {
    const value = await api('/api/runs', {method: 'POST', body: JSON.stringify({workflow_path: $('run-workflow').value, issue_id: $('run-issue').value ? Number($('run-issue').value) : null})});
    busy = true; scheduleRunRefresh();
    await Promise.all([showRun(value.run_id), loadHistory()]);
  } finally { startingRun = false; updateRunControls(); }
}
$('new-workflow').addEventListener('click', () => {
  creatingWorkflow = true; updateWorkflowSelection(); $('workflow-filename').focus();
});
$('cancel-workflow').addEventListener('click', () => {
  $('workflow-form').reset(); creatingWorkflow = false; updateWorkflowSelection();
});
$('workflow-form').addEventListener('submit', async event => {
  event.preventDefault();
  const buttons = ['create-workflow', 'cancel-workflow', 'new-workflow'].map($);
  buttons.forEach(button => { button.disabled = true; });
  try {
    let name = $('workflow-filename').value.trim();
    if (!name.endsWith('.md')) name += '.md';
    const created = await api('/api/workflows', {method: 'POST', body: JSON.stringify({path: '.whalehub/' + name})});
    await loadWorkflows(); await openWorkflow(created.path);
    const {files} = await api('/api/files'); renderFiles(files);
    $('workflow-form').reset(); error();
  } catch (e) { error(e.message); }
  finally { buttons.forEach(button => { button.disabled = false; }); }
});
$('save').addEventListener('click', () => save().catch(e => error(e.message)));
$('toggle-workflow-guide').addEventListener('click', () => {
  const guide = $('workflow-guide');
  guide.hidden = !guide.hidden;
  $('toggle-workflow-guide').setAttribute('aria-expanded', String(!guide.hidden));
});
$('workflow-test-form').addEventListener('submit', event => { event.preventDefault(); startWorkflowTest().catch(e => error(e.message)); });
for (const [field, id] of [['title', 'workflow-test-title'], ['body', 'workflow-test-body']]) {
  $(id).addEventListener('input', () => { if (opened) testFor(opened.path).issue[field] = $(id).value; });
}
$('discard-workflow').addEventListener('click', () => {
  if (opened?.dirty && window.confirm('Discard unsaved workflow edits?')) openWorkflow(opened.path, true).catch(e => error(e.message));
});
$('run-form').addEventListener('submit', event => { event.preventDefault(); startRun().catch(e => error(e.message)); });
$('new-run').addEventListener('click', () => newRun().catch(e => error(e.message)));
$('cancel-run').addEventListener('click', () => { creatingRun = false; showRunSelection(); });
$('run-workflow').addEventListener('change', updateRunControls);
$('run-issue').addEventListener('change', updateRunControls);
$('run-issue-link').addEventListener('click', () => { selectIssue(Number($('run-issue-link').dataset.issueId)); activateTab($('tab-issues')); });
window.addEventListener('beforeunload', event => { if ([...workflowDrafts.values()].some(file => file.dirty)) { event.preventDefault(); event.returnValue = ''; } });
async function init() {
  showTab();
  const creation = issueForm({title: 'Title', body: 'Issue description'}, 'Create issue', async values => {
    const issue = await api('/api/issues', {method: 'POST', body: JSON.stringify(values)});
    selectIssue(issue.id); await loadIssues();
    if (issue.triggered_runs.length) {
      const notice = issueCards.get(issue.id).automation;
      notice.textContent = issue.triggered_runs.length === 1 ? 'Workflow queued automatically: ' : `${issue.triggered_runs.length} workflows queued automatically: `;
      issue.triggered_runs.forEach((run, index) => {
        if (index) notice.append(', ');
        notice.append(runLink(run.run_id, run.name));
      });
      notice.hidden = false;
    }
    await loadHistory();
  });
  creation.actions.append(button('Cancel', async () => {
    creation.node.reset(); creatingIssue = false; showIssueSelection();
  }));
  $('new-issue-form').append(creation.node);
  $('new-issue').addEventListener('click', () => {
    creatingIssue = true; showIssueSelection(); creation.inputs.title.focus();
  });
  const [{files}] = await Promise.all([api('/api/files'), loadIssues(), loadWorkflows()]);
  renderFiles(files);
  if (files.length) await openCodeFile(files.includes('README.md') ? 'README.md' : files[0]);
  if (workflowItems.length && workflowGeneration === 0) await openWorkflow(workflowItems[0].path);
  const runs = await loadHistory();
  await loadWorkflowTests();
  if (runs.length) await showRun((runs.find(run => run.status === 'running') || runs[0]).run_id);
  else { creatingRun = true; showRunSelection(); }
}
init().catch(e => error(e.message));
