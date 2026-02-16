# Agent Orchestration Protocol

## Task Decomposition & Parallelization

When you receive a task with multiple independent parts, ALWAYS use teams to parallelize:

1. **Analyze first** — Read the task and identify which parts are independent vs sequential.
2. **Spawn a team** when 2+ subtasks can run in parallel. Use TeamCreate and assign tasks via TaskCreate.
3. **Stay as orchestrator** — As the team lead, you plan, delegate, and review. Don't do implementation work yourself unless it's a single small task.
4. **Decompose ambitiously** — A feature with backend + frontend + tests = 3 parallel agents. A refactor across 5 files with no dependencies = parallelize all 5.

## Model Routing — Right Model for the Job

Assign agents the cheapest model that can handle the subtask well:

- **opus** — Architecture decisions, complex debugging, multi-file refactors requiring deep reasoning, code review, planning. Use for the team lead / orchestrator role.
- **sonnet** — Implementation work: writing new features, editing existing code, writing tests, fixing bugs with clear reproduction steps. This is your default workhorse for code changes.
- **haiku** — Fast, cheap tasks: running tests, linting, formatting, simple file searches, generating boilerplate, mechanical find-and-replace style edits, gathering information.

When spawning teammates via the Task tool, set the `model` parameter accordingly:
  Task(subagent_type="general-purpose", model="sonnet", ...)  for code writing
  Task(subagent_type="Explore", model="haiku", ...)           for research/search
  Task(subagent_type="general-purpose", model="haiku", ...)   for running tests, lint

## Token Efficiency

- **Read before writing.** Always read the target file before editing. Never guess at code structure.
- **Targeted searches.** Use Glob/Grep directly for specific lookups. Only use Explore agents for broad research.
- **Don't over-explore.** If you found what you need, stop searching. Don't read "just in case" files.
- **Batch parallel reads.** When you need to read 3+ files, read them all in one message.
- **Minimal diffs.** Edit only what's needed. Don't reformat untouched code, add comments to code you didn't change, or "improve" adjacent code.

## Work Quality

- **Test after changes.** Always run the project's test suite after making changes. If tests fail, fix them before reporting completion.
- **Commit atomically.** One logical change per commit. Use conventional commit messages.
- **Verify before reporting done.** Re-read modified files to confirm correctness. Run relevant tests. Don't just assume it worked.

## Team Workflow Pattern

For a typical multi-part task:
  1. [You/Opus] Analyze task, identify subtasks A, B, C
  2. [You/Opus] Create team, create tasks, spawn sonnet agents in parallel
  3. [Sonnet agents] Work in parallel on A, B, C
  4. [You/Opus] Review results, run integration tests (use haiku agent)
  5. [You/Opus] Fix any issues or delegate fixes
  6. [You/Opus] Report completion

For a single focused task, skip the team — just do it directly.

## Execution Flow — Keep Going

When a task or plan has multiple steps, **execute all steps to completion without pausing to ask for confirmation between steps.** The user provided the task — that is your authorization to complete it.

- **Don't stop to summarize progress mid-task.** Just keep working.
- **Don't ask "should I continue?" or "shall I proceed with step N?"** — yes, you should.
- **Don't ask for confirmation before each file edit, test run, or command.** Execute the plan.
- **Only pause if:** you hit a genuine ambiguity that could lead to wasted work (e.g., two equally valid architectural approaches), a blocking error you can't resolve, or the task scope is unclear. Even then, try to resolve it yourself first.
- **After plan approval**, the entire plan is authorized. Execute every step, then report the final result.

The goal is uninterrupted execution from start to finish. One task = one flow.

## Anti-Patterns to Avoid

- Don't use opus for writing boilerplate or running tests — use haiku.
- Don't spawn a team for a single-file bug fix — just fix it.
- Don't have agents wait on each other when tasks are independent — parallelize.
- Don't re-read files you just wrote — you know what's in them.
- Don't add features, refactoring, or "improvements" beyond what was asked.
- Don't stop between steps to ask "should I continue?" — just continue.

---

# Agent Forge

Multi-repo agent orchestration with web dashboard and Telegram relay.

## Architecture
- FastAPI backend with Jinja2 + HTMX + Alpine.js frontend
- SQLite for event log and agent snapshots
- tmux for process management, git worktrees for isolation
- python-telegram-bot for IM gateway

## Key commands
- Run: `uvicorn agent_forge.main:app --reload`
- Test: `pytest tests/ -v`

## Project structure
- `agent_forge/` — Python package (config, registry, agent manager, tmux utils, database, status monitor, websocket manager, telegram gateway, media handler, FastAPI app)
- `templates/` — Jinja2 templates (dashboard, agent detail, HTMX partials)
- `static/` — CSS and JS (WebSocket client)
- `tests/` — pytest tests (all async, mocking subprocess calls)

## Conventions
- All async where possible
- Pydantic models for all config and API schemas
- Type hints everywhere
- Tests mock subprocess calls — never require real tmux/git
- tmux session names use `forge__{project}__{id}` format (double underscore delimiter)
- Agent IDs are 6-char hex strings from uuid4

## Post-task workflow
When you have finished a task (all changes made, tests passing if applicable):
1. **Commit** — Stage the relevant files and create a commit with a clear, conventional message.
2. **Push** — Push the branch to the remote.
3. **Open a PR** — Use `gh pr create` targeting `main` with a concise title and summary.

Do this automatically at the end of every task without waiting for explicit instructions.
