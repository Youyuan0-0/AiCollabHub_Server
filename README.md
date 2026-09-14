# AiCollabHub Server

人工智能协同平台的 FastAPI + SQLAlchemy 后端，包含首页问答、运营看板、统一身份登录和场景应用发布审核。

首页看板与多轮会话访问数据库；登录模块使用独立模拟 IAM，通过 CAS 票据建立持久会话；场景应用使用单实例内存保存发布、审核和个人互动。真实供应方 AI 和集团 CAS 尚未接入。

启用 `AUTH_MODE=mock` 后，首页、会话与场景应用接口共用登录检查，写请求校验 CSRF。场景应用的 `SessionIdentityProvider` 复用已验证用户身份，不允许通过演示账号切换绕过登录。

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
  api/                   版本化路由、健康检查、数据库看板与会话
  schemas/               看板与会话契约
  services/              快照聚合、会话存储与回复提供器
  data/                  看板及历史固定样例
  cache/                 Redis 客户端
  storage/               MinIO 客户端
  modules/
    datasets/            数据集目录样例
    models/              模型库目录样例
    applications/        场景应用与问答样例
    knowledge/           知识库、文档与检索样例
    tools/               AI 工具目录与调用样例
    common/              公共 DTO、样例加载、概览、反馈、申请和发布
      auth/              CAS 适配、共享会话、认证日志及独立模拟 IAM
    admin/               独立管理端的列表与处理样例
  main.py                应用工厂
migrations/              Alembic 基线、看板、会话及认证迁移
deploy/                  PostgreSQL、Redis、Nginx 配置及部署说明
scripts/                 启停、连接验证、主备验证和备份工具
tests/                   框架、Mock 契约及基础设施适配器测试
```

模块采用 `router.py`、`service.py` 和按需拆分的 `schemas.py`；目录型 DTO 和筛选逻辑复用公共模块。样例 JSON 按领域存放，读取时复制。场景应用由 `application_service.py` 处理业务规则、`memory_repository.py` 在每个 FastAPI 实例内单独暂存新增状态；`ports.py` 定义后续数据库/供应方替换合同；发布流程演示应直连单个实例、使用一个 worker，重启或代码热重载后重置，A/B 实例之间不共享记录。数据库持久化和多实例共享留待后续实现。

## 本地开发

推荐 Python 3.12 或 3.13，当前使用 3.13。运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

默认 `APP_ENV=development`。首页、会话与登录需要配置 `DATABASE_URL` 并执行迁移。正常登录联调设置 `AUTH_MODE=mock` 并启动独立 IAM，见 [认证联调说明](deploy/authentication.md)。`AUTH_MODE=disabled` 仅供显式匿名开发演示。真实凭据放在忽略的 `.env` 或环境变量。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/docs` | 开发接口文档，Swagger 资源使用上游 CDN |
| `GET /api/v1/framework` | Mock 模式、当前实例及模块清单 |
| `GET /api/v1/health/live` | 不依赖外部服务的存活检查 |
| `GET /api/v1/health/ready` | 真实主库连接检查；未配置时返回 503 |
| `GET /api/v1/health/dependencies` | 开发/测试环境的真实服务连接状态 |

Mock 接口只注册在 development/test 环境；数据库看板和会话在 production 返回 503，样例业务路由及依赖诊断不开放。production 要求配置数据库，当前没有正式业务 API。启动不会建表、建桶、修改复制配置或调用供应方。

## HTTP Mock 契约

默认前缀 `/api/v1`，JSON 字段使用 camelCase；响应带 `X-Request-ID`、`X-App-Instance`。看板和会话标记 `X-Data-Mode: development`，其他演示业务标记 `mock`；认证接口不标记业务数据模式。认证开关统一控制业务会话和 CSRF 检查。

| 路由 | 当前行为 |
| --- | --- |
| `GET /datasets`、`/models`、`/applications`、`/tools` | 样例目录，筛选、排序、分页 |
| `GET /overview/home`、`/overview/operations` | 数据库快照优先，未发布板块返回固定样例 |
| `/conversations`、`/conversations/new`、`/conversations/{id}/messages` | 共享开发历史、新建、续聊和删除，持久化到数据库 |
| `GET /applications/{id}` | 应用完整详情，包括 Markdown、教程、配置项和限制 |
| `GET/POST /application-publications`、`GET/PUT /application-publications/{id}` | 查询申请、新建/修改草稿、提交审核；内存暂存 |
| `GET /admin/application-publications` 及详情、`POST .../{id}/review` | 模拟审核；通过后进入同实例的应用目录 |
| `GET /admin/applications` 及详情、`PUT .../{id}/visibility` | 应用管理与有效的范围限制；关闭登录时用预置身份，启用后复用公共会话 |
| `GET /admin/application-review-queue` | 管理端审核查询、状态筛选、分页及数量 |
| `GET /application-form-configuration`、`GET/PUT /admin/application-configuration` | 表单必填和提示配置、管理列配置；提交校验与页面生效 |
| `/knowledge-bases` 及其子路径 | 知识库、分段、文件元信息、配置与检索样例 |
| `GET/POST /feedback` | 固定反馈列表、模拟提交回执 |
| `/resources/{id}/invoke`、`/resources/{id}/access-requests`、`/publications` | 模拟调用、申请及发布回执 |
| `GET /admin/overview`、`GET /admin/feedback`、`PATCH /admin/feedback/{id}` | 管理端概览、筛选分页和模拟处理回执 |

首页快照与多轮会话持久化到数据库，开发会话目前仍是共享历史。场景应用状态保存于当前后端实例，刷新保留、重启重置；发布与审核联调用一个 API 实例和一个 worker。看板 PUT 仍返回 503 `overview_publication_not_configured`，不能因为有登录会话而直接发布统计数据。其他模块写操作返回固定回执。详情见 [场景应用模块说明](app/modules/applications/README.md)。

这套接口用于验证传参、响应、错误、加载状态和组件交互。后续开发真实业务时再定义领域模型、身份权限、事务、文件状态和供应方适配，不把 Mock 回执当作实际业务验收。

## 基础设施

| 能力 | 项目已有内容 | 当前边界 |
| --- | --- | --- |
| PostgreSQL | 连接池、主库会话、显式只读路由、权重及受控连接回退 | 看板、对话和认证持久化；场景应用仍是单实例内存 |
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

迁移链为 `20260913_01`（基线）→ `20260914_01`（看板）→ `20260914_02`（会话）→ `20260914_03`（认证与旧分支兼容）。旧认证分支也曾使用 `20260914_01`，末次迁移会识别现有表，补齐缺失的看板或认证表并保留现有数据；两种分支的数据库均可执行 `upgrade head`，不需要删库或重新 stamp。未登记的历史表不会被自动生成迁移删除。

`compose.dev.yaml` 是可选的 PostgreSQL 17 单库开发配置，不包含主备。现有局域网测试集群为 PostgreSQL 16.14，服务配置与既有验证记录见 [部署说明](deploy/README.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
node scripts/verify_framework_browser.mjs
# AUTH_MODE=mock 的网关使用以下登录验证脚本：
node scripts/verify_auth_browser.mjs
```

Python 测试覆盖首页和会话持久化、场景应用发布审核、CAS 登录与退出、CSRF、接口拦截、基础设施和迁移兼容。浏览器回归验证桌面/手机的登录、首页问答和场景应用流程；匿名框架验证仅用于显式关闭认证的开发环境。

验证范围是框架和演示联通，不包括真实业务、Redis 服务、两台物理服务器容灾或 2 万在线/5000 并发容量。

- [部署与服务验证](deploy/README.md)
- [当前阶段任务](docs/current-task-priorities.md)
- [前端接口契约](../AiCollabHub_User/docs/api-contract-draft.md)

## 数据库看板与多轮会话

首次合并后安装 `python -m pip install -e ".[dev]" -c requirements-dev.lock`，以补齐
FastAPI 标准依赖与 Redis、MinIO、PostgreSQL 驱动；然后配置数据库并执行迁移。
也可使用 `fastapi dev`，入口由 `pyproject.toml` 声明。

会话为共享开发记录，没有登录和用户隔离。`create_app(chat_provider=...)` 可注入
第三方回复实现，默认回显；完整消息通过 `/conversations/{id}/messages` 读取。
附件仅由旧 multipart 提交入口兼容接收，不保存或参与回答；多轮 JSON 不支持附件。
数据库未配置、未迁移或不可用均返回错误，不回退成无状态 Mock。
