# Agent Orchestration Protocol

## Hard Rules — NEVER Violate

- **NEVER use Edit, Write, or NotebookEdit tools directly.** ALL code changes MUST be delegated to a sonnet agent via the Task tool.
- **NEVER run tests or builds directly.** Delegate to a haiku agent.
- **NEVER fix code yourself.** When issues are found during review or testing, delegate the fix to a sonnet agent. No exceptions.
- **You are the orchestrator (opus).** Your job is to analyze, plan, delegate, and review. You do NOT write code.

## Philosophy

- **Iterative delivery over massive releases** — Ship small, working slices of functionality.
- **Understand before you code** — Read existing code and explore patterns before making changes.
- **Pragmatism over ideology** — Choose what serves the project, not what's theoretically pure.
- **Readability over cleverness** — Optimize for the next developer reading your code.
- **Simplicity** — One clear responsibility per module. If it needs more than 3 sentences to explain, it's too complex.

## Decision Framework

When multiple solutions exist, prioritize in this order:

1. **Testability** — Can it be tested in isolation?
2. **Readability** — Will another dev understand this in 6 months?
3. **Consistency** — Matches existing patterns in the codebase?
4. **Simplicity** — Is this the least complex solution?
5. **Reversibility** — Can we swap it out easily later?

## Model Routing — Right Model for the Job

This applies to ALL tasks, not just parallelized ones. Every piece of work must use the right model:

- **opus (you)** — Architecture decisions, complex debugging, multi-file refactors requiring deep reasoning, code review, planning. You are the orchestrator. You analyze and delegate.
- **sonnet** — Implementation work: writing new features, editing existing code, writing tests, fixing bugs with clear reproduction steps. This is your default workhorse for ALL code changes.
- **haiku** — Fast, cheap tasks: running tests, linting, formatting, simple file searches, generating boilerplate, mechanical find-and-replace style edits, gathering information.

When spawning agents via the Task tool, set the `model` parameter accordingly:
  Task(subagent_type="general-purpose", model="sonnet", ...)  for code writing
  Task(subagent_type="Explore", model="haiku", ...)           for research/search
  Task(subagent_type="general-purpose", model="haiku", ...)   for running tests, lint

## Task Decomposition & Parallelization

When you receive a task with multiple independent parts, ALWAYS use teams to parallelize:

1. **Analyze first** — Read the task and identify which parts are independent vs sequential.
2. **Spawn a team** when 2+ subtasks can run in parallel. Use TeamCreate and assign tasks via TaskCreate.
3. **Stay as orchestrator** — As the team lead, you plan, delegate, and review.
4. **Decompose ambitiously** — A feature with backend + frontend + tests = 3 parallel agents. A refactor across 5 files with no dependencies = parallelize all 5.

## Available Agent Skills

Specialized agents are defined in `.claude/agents/`. See `.claude/agents/CATALOG.md` for the full directory with descriptions.

| Category | Key Agents | Use For |
|---|---|---|
| **Development** | `python-pro`, `backend-architect`, `full-stack-developer`, `frontend-developer` | Feature implementation, API design, UI |
| **Quality** | `code-reviewer`, `test-automator`, `debugger`, `architect-review` | Review, testing, debugging, architecture |
| **Data & AI** | `data-engineer`, `database-optimizer`, `ai-engineer`, `postgres-pro` | Data pipelines, DB tuning, AI features |
| **Infrastructure** | `cloud-architect`, `deployment-engineer`, `performance-engineer` | Cloud, CI/CD, performance |
| **Security** | `security-auditor` | Audits, vulnerability assessment |
| **Docs** | `api-documenter`, `documentation-expert` | API docs, technical writing |

### Default Agents for This Project

Agent Forge is Python/FastAPI. Default picks:
- **Implementation**: `python-pro` — FastAPI, async, Pydantic expertise
- **Architecture**: `backend-architect` — API design, system patterns
- **Testing**: `test-automator` — pytest, async test strategies
- **Review**: `code-reviewer` — Quality, security, best practices
- **Debugging**: `debugger` — Root cause analysis, error investigation
- **Orchestration**: `agent-organizer` — For unfamiliar domains, use to analyze requirements and recommend agent teams

## Team Composition Patterns

Common team formations for frequent task types:

**Bug Fix**: `debugger` (sonnet) → `python-pro` (sonnet) → tests (haiku) → review (opus)

**New Feature**: `backend-architect` (sonnet, design) + `python-pro` (sonnet, implement) in parallel → `test-automator` (sonnet, tests) → tests (haiku) → `code-reviewer` (haiku) → review (opus)

**Refactor**: `architect-review` (haiku, assess) → `python-pro` (sonnet, implement) → tests (haiku) → `code-reviewer` (haiku) → review (opus)

**Security Audit**: `security-auditor` (sonnet, audit) → `python-pro` (sonnet, fixes) → `security-auditor` (sonnet, re-verify)

**Cross-Stack Feature**: `backend-architect` (sonnet) + `frontend-developer` (sonnet) in parallel → consistency check (haiku) → tests (haiku) → review (opus)

## Technical Standards

- **Composition over inheritance** for components and service classes.
- **Contracts over direct calls** — Use type definitions, Pydantic models, and API specs.
- **Explicit data flow** — Document request/response shapes.
- **Fail fast** with descriptive error messages and meaningful log entries.
- **No silent catch blocks** — Handle errors at the right layer with context.

## Token Efficiency

- **Read before writing.** Always read the target file before editing. Never guess at code structure.
- **Targeted searches.** Use Glob/Grep directly for specific lookups. Only use Explore agents for broad research.
- **Don't over-explore.** If you found what you need, stop searching. Don't read "just in case" files.
- **Batch parallel reads.** When you need to read 3+ files, read them all in one message.
- **Minimal diffs.** Edit only what's needed. Don't reformat untouched code, add comments to code you didn't change, or "improve" adjacent code.

## Quality Gate — Required Pipeline

Every task MUST pass through this pipeline before completion. No shortcuts.

### For multi-agent tasks:

1. [Opus] Analyze task, identify subtasks, plan architecture
2. [Opus] Create team/tasks, spawn sonnet agents for implementation
3. [Sonnet agents] Implement in parallel
4. [Haiku agent] Run tests on the combined result
5. [Opus] Review the diff — read changed files and verify correctness
6. [Opus] If issues found: delegate fix to sonnet → re-test with haiku → review again. Repeat until clean.
7. [Opus] Only report completion after tests pass AND review is clean

### For single tasks:

1. [Haiku/Opus] Research — gather context with Explore agent or direct Glob/Grep
2. [Opus] Plan — decide the approach
3. [Sonnet] Implement — delegate the code changes
4. [Haiku] Test — run tests to verify
5. [Opus] Review — read the diff and verify correctness
6. [Opus] If issues found: delegate fix to sonnet → re-test with haiku → review again. Repeat until clean.
7. [Opus] Only report completion after tests pass AND review is clean

### Cross-agent consistency check:

When multiple sonnet agents work on related code (e.g. backend + frontend, API + client), spawn a haiku agent after both complete to verify:
- Shared types/interfaces match
- API contracts align between producer and consumer
- No conflicting assumptions between implementations

## When Stuck (Max 3 Attempts)

1. **Document failures** — Include error messages, stack traces, and what was tried.
2. **Research alternatives** — Look for similar solutions in the codebase or external references.
3. **Try a different layer** — Sometimes a backend bug is a config problem, or vice versa.
4. **Escalate** — After 3 failed attempts, report findings and ask for human guidance. Don't spin.

## Work Quality

- **Test after changes.** Always delegate test runs to a haiku agent after changes. If tests fail, delegate fixes to sonnet, then re-test. Iterate until green.
- **Commit atomically.** One logical change per commit. Use conventional commit messages.
- **Review before reporting done.** Read the changed files yourself (opus) to verify correctness. Do not trust agent output blindly.
- **Error handling.** Fail fast with descriptive messages. No silent catch blocks. Handle errors at the right layer.

## Anti-Patterns to Avoid

- Don't use opus for writing code or boilerplate — delegate to sonnet.
- Don't use opus or sonnet for executing tests — delegate to haiku.
- Don't spawn a team for a single-file bug fix — spawn a single sonnet agent instead.
- Don't have agents wait on each other when tasks are independent — parallelize.
- Don't re-read files you just wrote — you know what's in them.
- Don't add features, refactoring, or "improvements" beyond what was asked.
- Don't skip the test → review → fix loop. Every change gets tested and reviewed before completion.
- Don't trust a single agent's work without verification. Test it, review it, then report done.

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
4. **Fail on PR** — When PR creation fails check which gh account is necessary.

Do this automatically at the end of every task without waiting for explicit instructions.
