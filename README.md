# AiCollabHub Server

人工智能协同平台的 FastAPI + SQLAlchemy 后端框架。当前阶段只搭建用户端、独立管理端、模块化后端及部署基础，前后端通过 HTTP Mock 接口联通。

登录、权限、真实反馈持久化和业务文件模块已移除。页面使用后端提供的固定样例，提交、处理、发布、问答等操作返回模拟结果，不调用真实业务系统，也不写数据库、Redis 或 MinIO。服务连接和主备验证通过独立的健康检查与脚本执行。

## 技术与目录

- FastAPI、Pydantic：版本化 API、输入校验和 OpenAPI。
- SQLAlchemy 2、psycopg 3、Alembic：PostgreSQL 会话、事务、读路由与迁移基础。
- redis-py、MinIO SDK：缓存与对象存储适配器。
- Nginx：两个前端的静态资源、History 路由回退和两个后端实例分流。
- 达梦适配暂缓，按后续交付要求再启动。

```text
app/
  core/                  配置及实例资源生命周期
  db/                    连接池、会话、读路由、提交等待处理
  api/                   版本化路由与健康检查
  cache/                 Redis 客户端
  storage/               MinIO 客户端
  modules/
    datasets/            数据集目录样例
    models/              模型库目录样例
    applications/        场景应用与问答样例
    knowledge/           知识库、文档与检索样例
    tools/               AI 工具目录与调用样例
    common/              公共 DTO、样例加载、概览、反馈、申请和发布
    admin/               独立管理端的列表与处理样例
  main.py                应用工厂
migrations/              Alembic 环境和空基线
deploy/                  PostgreSQL、Redis、Nginx 配置及部署说明
scripts/                 启停、连接验证、主备验证和备份工具
tests/                   框架、Mock 契约及基础设施适配器测试
```

模块采用 `router.py`、`service.py` 和按需拆分的 `schemas.py`；目录型 DTO 和筛选逻辑复用公共模块。样例 JSON 按领域存放，读取时复制，避免请求间或 A/B 实例间混入修改状态。当前是模块化单体，可部署多个同版实例；不是每个目录单独运行一个微服务。

## 本地开发

推荐 Python 3.12 或 3.13，当前使用 3.13。运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

默认 `APP_ENV=development`，不需要外部服务就能运行全部样例页面接口。`.env.example` 是配置模板，真实密码仅放入忽略的 `.env` 或环境变量。用户端和管理端的 Vite 默认将 `/api` 代理到端口 8000。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/docs` | 开发接口文档，Swagger 资源使用上游 CDN |
| `GET /api/v1/framework` | Mock 模式、当前实例及模块清单 |
| `GET /api/v1/health/live` | 不依赖外部服务的存活检查 |
| `GET /api/v1/health/ready` | 真实主库连接检查；未配置时返回 503 |
| `GET /api/v1/health/dependencies` | 开发/测试环境的真实服务连接状态 |

Mock 接口只注册在 development/test 环境；production 不提供样例业务路由及依赖诊断，且要求配置数据库。当前没有正式业务 API。启动不会建表、建桶、修改复制配置或调用供应方。

## HTTP Mock 契约

默认前缀 `/api/v1`，JSON 字段使用 camelCase；响应带 `X-Request-ID`、`X-App-Instance`，样例接口另带 `X-Data-Mode: mock`。无登录、会话、CSRF 或权限依赖。

| 路由 | 当前行为 |
| --- | --- |
| `GET /datasets`、`/models`、`/applications`、`/tools` | 样例目录，筛选、排序、分页 |
| `GET /overview/home`、`/overview/operations` | 首页、Token 和算力看板样例 |
| `/conversations` | 固定历史样例、模拟回答和删除回执 |
| `/knowledge-bases` 及其子路径 | 知识库、分段、文件元信息、配置与检索样例 |
| `GET/POST /feedback` | 固定反馈列表、模拟提交回执 |
| `/resources/{id}/invoke`、`/resources/{id}/access-requests`、`/publications` | 模拟调用、申请及发布回执 |
| `GET /admin/overview`、`GET /admin/feedback`、`PATCH /admin/feedback/{id}` | 管理端概览、筛选分页和模拟处理回执 |

写操作不保存跨请求状态；再次查询或刷新恢复固定样例。用户端提交的反馈不会新增到管理端，管理端处理不会形成持久化回复。文件只传文件名、大小等元信息，不上传二进制。浏览器收藏仍是本机演示状态。

这套接口用于验证传参、响应、错误、加载状态和组件交互。后续开发真实业务时再定义领域模型、身份权限、事务、文件状态和供应方适配，不把 Mock 回执当作实际业务验收。

## 基础设施

| 能力 | 项目已有内容 | 当前边界 |
| --- | --- | --- |
| PostgreSQL | 连接池、主库会话、显式只读路由、权重及受控连接回退 | 页面 Mock 不访问数据库；当前 `DB_READ_ENABLED=false` |
| 同步重放 | `remote_apply` 校验、提交期限和不确定结果处理；主备配置及测试脚本 | 只对显式使用数据库事务的调用生效；Mock 提交不会触发复制 |
| Redis | 共享客户端、环境前缀、TTL、失败按未命中处理、Compose 模板 | 真实 Redis 服务待提供；没有业务缓存 |
| MinIO | 对象上传、读取、删除、大小检查、连接回收 | 基础适配器及服务探测；没有业务文件 API |
| Web 分流 | 用户端、独立管理端、A/B API 的本地启动与 Nginx 配置 | 已验证本地双进程，不等于两台物理服务器验收 |
| 备份 | 数据库逻辑备份和隔离恢复；指定对象前缀备份及新桶恢复 | 不依赖已移除的业务表；定时独立备份尚未部署 |

`SessionDependency` 用于主库事务，服务显式提交；`ReadSessionDependency` 仅用于纯查询。只读会话拒绝 ORM 写入、行锁与原始 SQL。读回退默认关闭，启用时使用独立且有界的主库只读连接池，仅在连接获取失败时尝试一次，不自动重放已执行查询。每个进程分别计算池预算。

“写入等待备库重放”需要 PostgreSQL 同时配置指定同步备库与 `synchronous_commit=remote_apply`。`DB_REQUIRE_SYNC_APPLY=true` 检查有效策略；`DB_COMMIT_TIMEOUT_MS` 为提交等待设置独立期限。取消或断连可能发生在本地提交之后，调用方必须处理 `commit_outcome_unknown`，不能当作已回滚。正式业务的幂等和冲突处理留到业务开发。

`CacheDependency` 未配置时返回 `None`，传输失败按缓存未命中处理；`StorageDependency` 未配置返回 503。适配器均为同步实现，应使用同步路由或工作线程。健康检查会实际访问服务，页面样例不会。

## 数据库迁移

数据库 URL 使用 `postgresql+psycopg://`。可选 `DATABASE_MIGRATION_URL` 指定迁移账号；迁移始终访问主库，每次发布单独执行一次，不在各 API 进程启动时运行。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe schema change"
```

当前 `20260913_01` 是空基线，不建立业务表；沿用版本号仅为兼容已运行过的测试库迁移标记。本轮没有删除远端历史测试表或文件。自动生成迁移会跳过仅在数据库中反射出来、未在当前模型中登记的表，避免把清理代码误当作删表授权；以后确需删除表时手工编写并审核迁移。

`compose.dev.yaml` 是可选的 PostgreSQL 17 单库开发配置，不包含主备。现有局域网测试集群为 PostgreSQL 16.14，服务配置与既有验证记录见 [部署说明](deploy/README.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
node scripts/verify_framework_browser.mjs
```

Python 测试覆盖配置、数据库路由及提交异常、缓存和对象客户端、各模块 Mock 契约。Mock 路由测试禁止所有数据库、Redis 和 MinIO 网络调用。浏览器脚本依赖用户端已安装的 Playwright、Chrome 及已启动的 18880 网关，覆盖桌面与手机的 10 个页面、无登录访问、模拟提交和 A/B 分流。

验证范围是框架和演示联通，不包括真实业务、Redis 服务、两台物理服务器容灾或 2 万在线/5000 并发容量。

- [部署与服务验证](deploy/README.md)
- [当前阶段任务](docs/current-task-priorities.md)
- [前端接口契约](../AiCollabHub_User/docs/api-contract-draft.md)
