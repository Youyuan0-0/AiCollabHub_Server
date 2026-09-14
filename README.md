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
- No production database, SSO protocol, AI provider, or business schema has been
  selected. SQLite is used only by automated tests.

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

An `.env` file is optional for the initial development server. Supported settings
are documented in `.env.example`; real credentials must not be committed.
`requirements-dev.lock` records the dependency versions verified locally. Update
it deliberately with a full test run; it is not a target-platform certification.

- API documentation: http://127.0.0.1:8000/docs
- Liveness: `GET /api/v1/health/live`
- Database readiness: `GET /api/v1/health/ready`
- Current session: `GET /api/v1/auth/session`
- SSO login URL: `GET /api/v1/auth/login-url?returnTo=/`

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

There are no business revisions yet. Import new model modules into the migration
environment before autogeneration so their tables are registered in
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
