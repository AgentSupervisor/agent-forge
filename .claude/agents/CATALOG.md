# Agent Skills Catalog

Specialized agents for team composition. Each agent is a `.md` file in `.claude/agents/` with YAML frontmatter (name, description, tools, model).

Reference this catalog when assembling teams. The orchestrator (opus) selects agents based on task requirements and spawns them via the Task tool.

---

## Development & Engineering

### Frontend & UI

| Agent | File | Model | Use For |
|---|---|---|---|
| `frontend-developer` | `development/frontend-developer.md` | sonnet | React, Vue, Angular, responsive design, component architecture |
| `ui-designer` | `development/ui-designer.md` | sonnet | Visual design, design systems, interface aesthetics |
| `ux-designer` | `development/ux-designer.md` | sonnet | Usability, accessibility, user research, interaction design |
| `react-pro` | `development/react-pro.md` | sonnet | Advanced React: hooks, context, performance, patterns |
| `nextjs-pro` | `development/nextjs-pro.md` | sonnet | Next.js: SSR, SSG, API routes, SEO optimization |

### Backend & Architecture

| Agent | File | Model | Use For |
|---|---|---|---|
| `backend-architect` | `development/backend-architect.md` | sonnet | API design, microservices, database schemas, system design |
| `full-stack-developer` | `development/full-stack-developer.md` | sonnet | End-to-end web apps, frontend + backend integration |

### Language & Platform Specialists

| Agent | File | Model | Use For |
|---|---|---|---|
| `python-pro` | `development/python-pro.md` | sonnet | Django, FastAPI, async, decorators, performance optimization |
| `golang-pro` | `development/golang-pro.md` | sonnet | Go concurrency, microservices, CLI tools, goroutines |
| `typescript-pro` | `development/typescript-pro.md` | sonnet | Advanced TypeScript, type safety, scalable architecture |
| `mobile-developer` | `development/mobile-developer.md` | sonnet | React Native, Flutter, native integrations, mobile UX |
| `electron-pro` | `development/electorn-pro.md` | sonnet | Electron desktop apps, native system integration |

### Developer Experience

| Agent | File | Model | Use For |
|---|---|---|---|
| `dx-optimizer` | `development/dx-optimizer.md` | sonnet | Tooling, build systems, dev workflows, productivity |
| `legacy-modernizer` | `development/legacy-modernizer.md` | sonnet | Legacy refactoring, gradual modernization, framework migration |

---

## Quality & Testing

| Agent | File | Model | Use For |
|---|---|---|---|
| `code-reviewer` | `quality-testing/code-reviewer.md` | haiku | Code quality, security review, best practices, mentoring feedback |
| `architect-review` | `quality-testing/architect-review.md` | haiku | Architectural consistency, SOLID compliance, dependency analysis |
| `test-automator` | `quality-testing/test-automator.md` | haiku | Test strategy, unit/integration/E2E tests, CI/CD integration |
| `qa-expert` | `quality-testing/qa-expert.md` | haiku | Quality processes, testing strategy, reliability standards |
| `debugger` | `quality-testing/debugger.md` | sonnet | Root cause analysis, error investigation, test failure diagnosis |

---

## Data & AI

| Agent | File | Model | Use For |
|---|---|---|---|
| `data-engineer` | `data-ai/data-engineer.md` | sonnet | ETL pipelines, data warehouses, streaming, data processing |
| `data-scientist` | `data-ai/data-scientist.md` | sonnet | SQL, BigQuery, statistical analysis, business intelligence |
| `database-optimizer` | `data-ai/database-optimizer.md` | sonnet | Query optimization, indexing, schema design, migration |
| `postgres-pro` | `data-ai/postgres-pro.md` | sonnet | PostgreSQL advanced queries, tuning, PG-specific features |
| `graphql-architect` | `data-ai/graphql-architect.md` | sonnet | GraphQL schemas, resolvers, federation, performance |
| `ai-engineer` | `data-ai/ai-engineer.md` | sonnet | LLM apps, RAG systems, prompt pipelines, AI API integration |
| `ml-engineer` | `data-ai/ml-engineer.md` | sonnet | ML pipelines, model serving, feature engineering, deployment |
| `prompt-engineer` | `data-ai/prompt-engineer.md` | sonnet | Prompt optimization, LLM tuning, AI system effectiveness |

---

## Infrastructure & Operations

| Agent | File | Model | Use For |
|---|---|---|---|
| `cloud-architect` | `infrastructure/cloud-architect.md` | sonnet | AWS/Azure/GCP, cloud-native, cost optimization |
| `deployment-engineer` | `infrastructure/deployment-engineer.md` | sonnet | CI/CD, Docker, Kubernetes, infrastructure automation |
| `performance-engineer` | `infrastructure/performance-engineer.md` | sonnet | Bottleneck analysis, caching, monitoring, optimization |
| `devops-incident-responder` | `infrastructure/devops-incident-responder.md` | sonnet | Production issues, log analysis, deployment troubleshooting |
| `incident-responder` | `infrastructure/incident-responder.md` | sonnet | Critical outages, crisis management, escalation, post-mortems |

---

## Security

| Agent | File | Model | Use For |
|---|---|---|---|
| `security-auditor` | `security/security-auditor.md` | sonnet | Vulnerability assessment, penetration testing, OWASP compliance |

---

## Documentation & Specialization

| Agent | File | Model | Use For |
|---|---|---|---|
| `api-documenter` | `specialization/api-documenter.md` | sonnet | OpenAPI/Swagger specs, SDK guides, API reference |
| `documentation-expert` | `specialization/documentation-expert.md` | sonnet | Technical writing, user manuals, knowledge bases |

---

## Business & Strategy

| Agent | File | Model | Use For |
|---|---|---|---|
| `product-manager` | `business/product-manager.md` | sonnet | Product roadmaps, market analysis, business-tech alignment |

---

## Orchestration

| Agent | File | Model | Use For |
|---|---|---|---|
| `agent-organizer` | `agent-organizer.md` | haiku | Project analysis, team recommendation, delegation strategy |

Use `agent-organizer` when facing an unfamiliar domain or complex multi-domain task. It analyzes the project and recommends which agents to assemble. The orchestrator (opus) makes the final decision.

---

## Model Assignment Guide

Agents have a `model` field in their frontmatter. When spawning via Task tool, override as needed:

- **sonnet** — Default for all implementation agents (writing/editing code)
- **haiku** — Use for review, analysis, testing, and quick lookups
- **opus** — Reserved for the orchestrator; never spawn opus sub-agents

The model in the agent file is a suggestion. The orchestrator always has final say based on task complexity.

## Agent File Format

Each agent `.md` file follows this structure:

```yaml
---
name: agent-name
description: One-line description of capabilities
tools: Read, Write, Edit, Grep, Glob, Bash, ...
model: sonnet
---
```

Followed by a detailed markdown prompt with:
- **Role** and expertise description
- **Core competencies** and specialized behaviors
- **Standard operating procedure**
- **Output format** requirements
