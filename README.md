# AiCollabHub Server

FastAPI and SQLAlchemy backend for the AI collaboration platform. This is the
initial modular-monolith foundation, not an implementation of AI tools or RAG.
The user portal and administration frontend remain separate applications.

## Technology decisions

- FastAPI for HTTP APIs and OpenAPI documentation.
- SQLAlchemy 2 for database access and Alembic for schema migrations.
- Pydantic Settings for environment configuration.
- Synchronous SQLAlchemy sessions initially, to simplify database-driver
  compatibility checks. Keep blocking database work in synchronous routes or
  worker threads; future asynchronous AI calls and streaming can be added
  independently.
- No production database, SSO protocol or AI provider has been selected.
  SQLite is used for isolated tests and local dashboard integration only.

## Local development

Python 3.12 or 3.13 is recommended initially. The local scaffold was tested with
Python 3.13; the target OS, CPU architecture, Python version and database driver
must be validated together before deployment.

Run from this directory in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

也可以在项目根目录激活虚拟环境后使用 FastAPI CLI：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]" -c requirements-dev.lock
fastapi dev
```

项目通过 `fastapi[standard]` 安装 CLI 依赖，并在 `pyproject.toml` 中指定
`app.main:app` 为入口。已有虚拟环境需要重新执行上述安装命令；若提示安装
`fastapi[standard]`，说明当前环境尚未安装 CLI 依赖。

An `.env` file is optional for the initial development server. Supported settings
are documented in `.env.example`; real credentials must not be committed.
`requirements-dev.lock` records the dependency versions verified locally. Update
it deliberately with a full test run; it is not a target-platform certification.

- API documentation: http://127.0.0.1:8000/docs
- Liveness: `GET /api/v1/health/live`
- Database readiness: `GET /api/v1/health/ready`
- Current session: `GET /api/v1/auth/session`
- SSO login URL: `GET /api/v1/auth/login-url?returnTo=/`
- Home dashboard: `GET /api/v1/overview/home`
- Operations dashboard: `GET /api/v1/overview/operations`

首页与运营基础接口已实现数据库快照持久化和受权限控制的发布接口；资源数量、Token、
算力与榜单尚未接入实时业务统计。字段、筛选、迁移及前端联调说明见
[首页看板接口文档](docs/overview-api.md)。非生产环境仅两个看板 GET 可匿名读取；
生产读取需要 `overview:read`，所有发布均需要可信身份及 `overview:read`、
`overview:write`。暂不实现登录，后续对接第三方单点登录。
开发环境尚未发布的板块会返回原前端的固定展示内容（126 个智能体、92 个数据集、
48 个模型等），由 `metadata.demoSections` 标记；已发布快照优先，生产不使用样例。

Without `DATABASE_URL`, liveness succeeds and readiness returns HTTP 503 with
`database_not_configured`. No database is created automatically. Production mode
requires `DATABASE_URL` and disables Swagger UI and the public OpenAPI endpoint.
The development Swagger UI uses upstream CDN assets and is not an offline
documentation deployment.

The existing Vue development proxy forwards `/api` to port 8000, so CORS is not
required. For a separate browser origin, configure an explicit `CORS_ORIGINS`
JSON array. A fail-closed authentication extension point is available, but real
SSO, session persistence, logout, cross-origin cookies and CSRF policy are not
implemented.

## Authentication boundary

`create_app(identity_provider=...)` accepts a trusted `IdentityProvider` whose
implementation must verify the actual server-side session or token. No provider
is installed by default: session lookup returns 401 and login URL lookup returns
503. Browser-supplied role headers and unverified Bearer values never create a
user or administrator. Login return paths must be local paths.

Use `PrincipalDependency` or `Depends(require_permissions(...))` explicitly on
protected business routes. The permission dependency is tested, but this does
not implement complete RBAC, organization scopes or resource ACLs. Business
routes still need their own authorization and ownership checks when added.

## Structure

```text
app/
  api/          Versioned routes, health probes and authentication endpoints
  core/         Environment settings and trusted identity/permission contract
  db/           Declarative model base and request-scoped sessions
  schemas/      首页与运营接口契约及输入校验
  services/     看板快照聚合与持久化事务
  main.py       Application factory and engine lifecycle
migrations/     Alembic environment and future revisions
tests/          Configuration, HTTP and database tests
```

Add business modules when their contracts are defined. External systems should
be accessed through provider-specific adapters behind platform-owned interfaces;
do not expose upstream credentials or assume provider protocols in browser code.

## Database and migrations

Install the selected database's DBAPI driver and SQLAlchemy dialect, then
configure `DATABASE_URL`. The foundation does not install a vendor driver.
Dameng and other domestic databases still require concrete driver, dialect, SQL,
migration and target-platform validation; changing the connection URL is not a
compatibility guarantee.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe schema change"
```

The first business revision creates `overview_snapshots` without demo data.
Import new model modules into the migration environment before autogeneration
so their tables are registered in
`Base.metadata`. Review generated revisions and test upgrades on the target
database. Application startup never calls `create_all` or applies migrations.

Use `SessionDependency` in synchronous route handlers. Services commit at the
business transaction boundary; uncommitted changes are rolled back when the
request-scoped session closes. Readiness checks database connectivity only; it
does not certify schema versions or external-provider availability.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Tests use temporary SQLite files, not a production-database substitute. No
domestic database or application-server compatibility claim is made by these
tests. Python services cannot be treated as Java WAR applications merely by
packaging them in Docker; the final deployment contract is still pending.

## Phase-one delivery

The Vue portal has separate Mock and HTTP adapters. Its resource, knowledge,
chat, feedback and publication endpoints are currently contract drafts, not
implemented backend business APIs. Production database selection, persisted
business workflows, the independent admin frontend and the minimum multi-service
runtime remain pending. This modular application is not itself a delivered
distributed microservice system.

- [Phase-one task checklist](docs/phase-1-task-checklist.md)
- [Frontend API contract draft](../AiCollabHub_User/docs/api-contract-draft.md)

## 开发问答与历史

问答与历史已提供非生产固定演示接口，支持提交、列表、详情和删除。升级数据库后重启服务，详见 [问答接口说明](docs/conversations-api.md)。

临时会话服务已支持新建空对话、追加消息、完整历史和删除；存储通过 ConversationStore 扩展，第三方回复通过 create_app(chat_provider=...) 注入，默认仍原样回显。复用现有 JSON 表，无需新迁移，详见 docs/conversations-api.md。
