# AiCollabHub Server

人工智能协同平台的 FastAPI + SQLAlchemy 后端，包含首页问答、运营看板、统一身份登录和场景应用发布审核。保留用户端、独立管理端、模块化后端及部署基础，其余模块通过 HTTP Mock 接口联通。

看板读取数据库快照，未发布板块补充固定样例；会话支持空对话、追加消息、历史及删除并持久化，默认原样回显问题。不调用真实业务系统，服务连接和主备验证通过独立的健康检查与脚本执行。

可选的 CAS 登录联调使用模拟 IAM 提供一次性票据，平台用 PostgreSQL 保存共享会话、登录请求与认证日志。未建设个人资料页、完整用户档案、身份同步或正式角色权限。

场景应用支持两种存储方式：`memory` 为单实例临时演示，`database` 将平台自己的关联、权限、互动、审核补充项、消息和操作记录写入 PostgreSQL 的 8 张场景表。供应方目录、草稿、版本、发布和调用仍为开发模拟；认证模块按独立配置启用。

启用 `AUTH_MODE=mock` 时，`require_business_identity()` 校验会话及写请求的 CSRF，并把可信身份交给 `SessionIdentityProvider`。场景应用据此保存发布者和审核者标识，用户端和管理端分别读取 `/application-session` 与 `/admin/application-session`。此时关闭演示账号切换，不使用演示组织或项目替代尚未接入的 IAM 归属信息。`AUTH_MODE=disabled` 时继续使用原有演示账号。

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
    applications/        场景应用目录、发布及工作流演示
    knowledge/           知识库、文档与检索样例
    tools/               AI 工具目录与调用样例
    common/              公共 DTO、样例加载、概览、反馈、申请和发布
      auth/              CAS 适配、共享会话、认证日志及独立模拟 IAM
    admin/               独立管理端的列表与处理样例
  main.py                应用工厂
migrations/              Alembic 基线、看板、会话、认证及身份同步表迁移
deploy/                  PostgreSQL、Redis、Nginx 配置模板
scripts/                 启停、连接验证、主备验证和备份工具
tests/                   框架、Mock 契约及基础设施适配器测试
```

模块采用 `router.py`、`service.py` 和按需拆分的 `schemas.py`；目录型 DTO 和筛选逻辑复用公共模块。场景应用由 `application_service.py` 处理业务规则，`database_repository.py` 组合平台表和开发用外部资料快照。`memory_repository.py` 保留独立内存演示。`ports.py` 定义现有业务存储和运行适配边界，真实 HiMarket 的目录、草稿、版本和审批还需专门适配。

## 本地开发

推荐 Python 3.12 或 3.13，当前使用 3.13。运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

默认 `APP_ENV=development`、`AUTH_MODE=disabled`。看板和会话需要配置数据库并执行迁移；其余匿名 Mock 接口不依赖外部服务。启用 `AUTH_MODE=mock` 后，看板、会话和其他业务接口都要求有效登录会话，写请求校验 CSRF；需要执行完整迁移并单独运行模拟 IAM。

所有本地配置统一放在项目根目录 `.env`，包括 `POSTGRES_*`、`REDIS_*`、`MINIO_*` 和 `AUTH_*`；数据库和 Redis 支持分项账号密码，兼容显式连接 URL，显式 URL 优先。`.env.example` 是可提交的配置模板，真实密码仅放入忽略的 `.env` 或环境变量。用户端和管理端的 Vite 默认将 `/api` 代理到端口 8000。

场景应用使用已创建的表时，在 `.env` 设置：

```dotenv
APPLICATIONS_STORAGE=database
APPLICATIONS_DEMO_DIRECTORY=D:/UGit/AiCollabHub_Server/.runtime/application-marketplace-demo
```

目录按实际机器调整；同机多个后端须指向同一绝对路径及数据库。数据库模式复用主库连接池，读写均走主库，不自动建表，不把缺表或连接失败降级为内存成功。新环境需先执行场景建表 SQL；已建环境不可重复执行。配置在后端下次启动时读取，已有内存演示记录不会自动迁移。

外部模拟资料单独保存在上述目录，数据库仅保存引用和本地业务数据。模拟快照是开发持久化的一部分，清空目录会导致已有模拟草稿/版本无法读取；SQL 回滚可能留下未被引用的快照，不会提前发布。该方式支持同机重启及多进程共享，不代表不同服务器已共享外部资料。真实接入仍需 HiMarket/Agent Dev 适配及联调，不能只换地址。

集团真实 CAS 签名和接入信息尚未提供，因此没有生产认证模式，模拟 IAM 在 production 环境禁止启用。配置 PostgreSQL、执行迁移并构建两套前端后，可使用以下脚本启动认证联调；生成器统一更新根目录 `.env`，保留已有服务凭据和联调密钥。

```powershell
.\.venv\Scripts\python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
.\scripts\start-local.ps1 -NginxRoot <已解压的Nginx目录> -ApiPortA 18000 -ApiPortB 18001 -GatewayPort 18880 -MockIamPort 18002
```

用户端入口为 `http://127.0.0.1:18880/`，管理端为 `/admin/`。API 禁用带查询串的访问日志，避免记录 CAS 票据；停止服务使用 `scripts/stop-local.ps1 -ProcessRecord <启动输出的记录路径>`。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/docs` | 开发接口文档，Swagger 资源使用上游 CDN |
| `GET /api/v1/framework` | Mock 模式、当前实例及模块清单 |
| `GET /api/v1/health/live` | 不依赖外部服务的存活检查 |
| `GET /api/v1/health/ready` | 真实主库连接检查；未配置时返回 503 |
| `GET /api/v1/health/dependencies` | 开发/测试环境的真实服务连接状态 |

Mock 接口只注册在 development/test 环境；数据库看板和会话在 production 返回 503，样例业务路由及依赖诊断不开放。production 要求配置数据库，当前没有正式业务 API。启动不会建表、建桶、修改复制配置或调用供应方。

## HTTP Mock 契约

默认前缀 `/api/v1`，JSON 字段使用 camelCase；响应带 `X-Request-ID`、`X-App-Instance`，样例业务接口另带 `X-Data-Mode: mock`，数据库看板与会话使用 `X-Data-Mode: development`。场景数据库模式另标记 `X-Data-Mode: development`、`X-Application-Storage: postgresql`、`X-Application-Provider: simulation`。认证开关控制会话及 CSRF 校验；认证接口不标记为固定样例数据。

| 路由 | 当前行为 |
| --- | --- |
| `GET /datasets`、`/models`、`/applications`、`/tools` | 样例目录，筛选、排序、分页 |
| `GET /overview/home`、`/overview/operations` | 数据库快照优先，未发布板块返回固定样例 |
| `/conversations`、`/conversations/new`、`/conversations/{id}/messages` | 共享开发历史、新建、续聊和删除，持久化到数据库 |
| `GET /applications/{id}` | 应用完整详情，包括 Markdown、教程、配置项和限制 |
| `GET/POST /application-publications`、`GET/PUT /application-publications/{id}` | 查询申请、新建/修改草稿、提交模拟审核；按存储模式保存 |
| `GET /admin/application-publications` 及详情、`POST .../{id}/review` | 模拟审核；通过后进入模拟目录；数据库模式持久化本地评分和操作 |
| `GET /admin/applications` 及详情、`PUT .../{id}/visibility` | 应用管理与有效的范围限制；关闭登录时用预置身份，启用后复用公共会话 |
| `GET /admin/application-review-queue` | 管理端审核查询、状态筛选、分页及数量 |
| `GET /application-form-configuration`、`GET/PUT /admin/application-configuration` | 表单必填和提示配置、管理列配置；提交校验与页面生效 |
| `/knowledge-bases` 及其子路径 | 知识库、分段、文件元信息、配置与检索样例 |
| `GET/POST /feedback` | 固定反馈列表、模拟提交回执 |
| `/resources/{id}/invoke`、`/resources/{id}/access-requests`、`/publications` | 模拟调用、申请及发布回执 |
| `GET /admin/overview`、`GET /admin/feedback`、`PATCH /admin/feedback/{id}` | 管理端概览、筛选分页和模拟处理回执 |

看板 PUT 在完成登录及 CSRF 校验后仍返回 503 `overview_publication_not_configured`，接入可信发布方后再开放。会话数据持久化，仍为共享开发历史，尚未实现用户数据隔离。

场景应用在数据库模式下保留刷新、重启后的平台业务记录，目录 GET 不会自动把样例导入数据库；内存模式仍在重启后重置。其他模块仍为固定回执，例如用户端反馈不会新增到管理端。文件只传文件名、大小等元信息，不上传二进制；会话兼容入口接收但不保存附件。场景应用按账号保存收藏、关注新版和评分；其他目录收藏仍是本机演示状态。场景表结构见 `database/scenario-applications/schema.sql`。

这套接口用于验证传参、响应、错误、加载状态和组件交互。后续开发真实业务时再定义领域模型、身份权限、事务、文件状态和供应方适配，不把 Mock 回执当作实际业务验收。

## 基础设施

| 能力 | 项目已有内容 | 当前边界 |
| --- | --- | --- |
| PostgreSQL | 连接池、主库会话、显式只读路由、权重及受控连接回退 | 看板、会话、启用后的认证和场景数据库模式访问主库 |
| 同步重放 | `remote_apply` 校验、提交期限和不确定结果处理；主备配置及测试脚本 | 只对显式使用数据库事务的调用生效；Mock 提交不会触发复制 |
| Redis | 共享客户端、环境前缀、TTL、失败按未命中处理、Compose 模板 | 真实 Redis 服务待提供；没有业务缓存 |
| MinIO | 对象上传、读取、删除、大小检查、连接回收 | 基础适配器及服务探测；没有业务文件 API |
| Web 分流 | 用户端、独立管理端、A/B API 的本地启动与 Nginx 配置 | 已验证本地双进程，不等于两台物理服务器验收 |
| 备份 | 数据库逻辑备份和隔离恢复；指定对象前缀备份及新桶恢复 | 不依赖已移除的业务表；定时独立备份尚未部署 |

`SessionDependency` 用于主库事务，服务显式提交；`ReadSessionDependency` 仅用于纯查询。只读会话拒绝 ORM 写入、行锁与原始 SQL。读回退默认关闭，启用时使用独立且有界的主库只读连接池，仅在连接获取失败时尝试一次，不自动重放已执行查询。每个进程分别计算池预算。

“写入等待备库重放”需要 PostgreSQL 同时配置指定同步备库与 `synchronous_commit=remote_apply`。`DB_REQUIRE_SYNC_APPLY=true` 检查有效策略；`DB_COMMIT_TIMEOUT_MS` 为提交等待设置独立期限。取消或断连可能发生在本地提交之后，调用方必须处理 `commit_outcome_unknown`，不能当作已回滚。正式业务的幂等和冲突处理留到业务开发。

`CacheDependency` 未配置时返回 `None`，传输失败按缓存未命中处理；`StorageDependency` 未配置返回 503。适配器均为同步实现，应使用同步路由或工作线程。健康检查会实际访问服务；数据库看板、会话、认证和场景数据库模式也访问数据库。

## 数据库迁移

数据库可使用 `POSTGRES_HOST/PORT/DB/USER/PASSWORD` 分项配置，兼容 `postgresql+psycopg://` 形式的 `DATABASE_URL`。可选 `POSTGRES_MIGRATION_*` 或 `DATABASE_MIGRATION_URL` 指定迁移账号；迁移始终访问主库，每次发布单独执行一次，不在各 API 进程启动时运行。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe schema change"
```

迁移链为 `20260913_01`（空基线）→ `20260914_01`（overview_snapshots）→ `20260914_02`（demo_conversations）→ `20260914_03`（认证三表及旧分支兼容）→ `20260915_01`（本地历史版本兼容标记）→ `20260915_02`（身份同步三表）。

最早两个分支曾用同一个 `20260914_01` 分别创建看板表和认证三表，`20260914_03` 会补齐缺表并保留已有数据。之后本地与远端分别用 `20260915_01` 和 `20260914_03` 建立了相同结构，因此保留两者编号，实际建表统一由 `20260914_03` 完成，`20260915_01` 只保留版本标记。已有任一版本的数据库执行 `upgrade head` 即可，无需手工 stamp 或删表；从 `20260915_01` 回退到 `20260914_03` 不修改表，继续回退到 `20260914_02` 才删除认证三表。

认证三表为 `auth_session`、`auth_login_request`、`auth_event_log`。自动生成迁移会跳过仅在数据库中反射出来、未在当前模型中登记的表，避免误删历史测试表；确需删除表时手工编写并审核迁移。

`20260915_02` 补回远端已经使用、当前代码此前缺失的迁移脚本，包含 `iam_identity_state`、`iam_identity_role`、`iam_sync_receipt`。已核对列类型、空值约束、主外键、检查约束和索引与远端一致。已经处于 02 的数据库执行 `upgrade head` 会直接完成，不重建表或改写版本号；01 的数据库正常创建这三张表。如果 01 的库已手工创建同名表，应先核对来源和结构，不能直接 stamp 跳过。该修复仅恢复迁移历史，不表示当前后端已接入真实 IAM 同步接口。

2026-09-15 已在远端执行 `alembic upgrade head`，本地迁移、主库与只读副本均为 `20260915_02`。本次执行没有产生 DDL 或业务写入，原有 17 张表保留；记录见 `database/migration-alignment-20260915.json`。新增 6 项身份同步迁移测试，迁移专项共 18 项、完整测试共 246 项通过。

场景 8 表及 2 视图已于 2026-09-15 通过独立 `database/scenario-applications/schema.sql` 在当前远端库创建，未修改 Alembic 版本。新库的 `alembic upgrade head` 不会创建场景表；后续纳入迁移链时须兼容已建结构，不能直接重复 CREATE 或用 stamp 冒充执行。

`compose.dev.yaml` 是可选的 PostgreSQL 17 单库开发配置，不包含主备；主备、Redis 和 Nginx 模板位于 `deploy/`。详细部署笔记和需求资料保留在本地，按 `.gitignore` 不纳入仓库。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
node scripts/verify_framework_browser.mjs
# AUTH_MODE=mock 的网关使用以下登录验证脚本：
node scripts/verify_auth_browser.mjs
```

Python 测试覆盖配置、数据库路由及提交异常、缓存和对象客户端、各模块 Mock 契约，以及模拟 CAS、跨实例会话、单点登出、CSRF、迁移。匿名 Mock 契约测试禁止外部服务调用，看板、会话和认证使用独立临时库，迁移测试覆盖两个历史分支升级及数据保留。浏览器脚本依赖用户端已安装的 Playwright、Chrome 及已启动的 18880 网关；框架脚本用于认证关闭的环境，认证脚本验证桌面/手机登录、原页面返回、管理端准入、Mock 写请求和 A/B 会话及登出一致性。

验证范围是框架和演示联通，不包括真实业务、Redis 服务、两台物理服务器容灾或 2 万在线/5000 并发容量。

`test_application_database_storage.py` 另有 10 项数据库集成测试，覆盖重启、重提历史、权限/配置共享、审核回滚、通知、运行/下载/同步去重、缺表报错和中断请求。只允许显式指定的本机 `55491` 端口 PGlite 隔离实例；测试会校验引擎并重置该实例，未配置时跳过，不读取项目远端连接。2026-09-15 在隔离实例下全套 **240 项通过**；PGlite 的多连接测试不等于真实 PostgreSQL 并发容量验证。另在远端真实表验证写入与新仓储读取，使用外层事务全部回滚，未提交测试业务记录。

## 数据库看板与多轮会话

首次合并后安装 `python -m pip install -e ".[dev]" -c requirements-dev.lock`，以补齐
FastAPI 标准依赖与 Redis、MinIO、PostgreSQL 驱动；然后配置数据库并执行迁移。
也可使用 `fastapi dev`，入口由 `pyproject.toml` 声明。

会话为共享开发记录，按配置校验登录，但尚无用户数据隔离。`create_app(chat_provider=...)` 可注入
第三方回复实现，默认回显；完整消息通过 `/conversations/{id}/messages` 读取。
附件仅由旧 multipart 提交入口兼容接收，不保存或参与回答；多轮 JSON 不支持附件。
数据库未配置、未迁移或不可用均返回错误，不回退成无状态 Mock。
