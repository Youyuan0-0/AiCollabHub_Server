# AiCollabHub Server

人工智能协同平台的 FastAPI + SQLAlchemy 后端，包含首页问答、运营看板、统一身份登录和场景应用发布审核。保留用户端、独立管理端、模块化后端及部署基础，其余模块通过 HTTP Mock 接口联通。

部署方式统一为单服务器：一份用户端、一份独立管理端、一个 Nginx 入口、一套多 worker 的 FastAPI 服务，以及 PostgreSQL、Redis、MinIO 各一个实例。所有数据库读写访问同一实例，仓库只维护这套部署方式。

看板读取数据库快照，未发布板块补充固定样例；会话支持空对话、追加消息、历史及删除并持久化，默认原样回显问题。不调用真实业务系统，服务连接通过健康检查与独立脚本验证。

可选的 CAS 登录联调使用模拟 IAM 提供一次性票据，平台用 PostgreSQL 保存共享会话、登录请求与认证日志。最小身份同步支持模拟用户状态和平台管理员授权变更，尚未接入集团身份推送、正式角色映射或完整用户档案，不建设个人资料页。

用户端和管理端通过同一网关使用 `ach_session` HttpOnly Cookie（路径 `/api`），任一端登录后另一端直接沿用身份，管理端仍逐次校验管理员准入。任一端退出、会话过期或收到单点登出通知后，两端同时失效；切换账号同步替换两端身份。兼容旧的分端 Cookie，但不延长旧会话有效期；旧 Cookie 对应不同账号时要求重新选择账号。

后端在后续请求中检查会话有效性；前端在路由切换、获得焦点和可见页面每 30 秒的检查中更新显示。账号状态的后台变化尚未通过实时推送通知浏览器。

场景应用业务使用单实例临时存储，可演示发布、审核、同步和调用等流程；这部分不连接数据库或供应方，认证模块按独立配置启用。

启用 `AUTH_MODE=mock` 时，`require_business_identity()` 校验会话及写请求的 CSRF，并把可信身份交给 `SessionIdentityProvider`。场景应用据此保存发布者和审核者标识，用户端和管理端分别读取 `/application-session` 与 `/admin/application-session`。此时关闭演示账号切换，不使用演示组织或项目替代尚未接入的 IAM 归属信息。`AUTH_MODE=disabled` 时继续使用原有演示账号。

## 技术与目录

- FastAPI、Pydantic：版本化 API、输入校验和 OpenAPI。
- SQLAlchemy 2、psycopg 3、Alembic：PostgreSQL 连接池、会话、事务与迁移。
- redis-py、MinIO SDK：缓存与对象存储适配器。
- Nginx：两个前端的静态资源、History 路由回退和单个 API 监听端口的反向代理。
- 达梦适配暂缓，按后续交付要求再启动。

```text
app/
  core/                  配置及实例资源生命周期
  db/                    连接池、会话、事务及持久化模型
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
      identity_sync/     最小身份状态、管理员授权、版本处理和同步回执
    admin/               独立管理端的列表与处理样例
  main.py                应用工厂
migrations/              Alembic 基线、看板、会话、认证及身份同步迁移
compose.yaml             PostgreSQL、Redis、MinIO 单服务器服务模板
deploy/                  Redis、Nginx 配置模板
scripts/                 启停、连接验证和备份工具
tests/                   框架、Mock 契约及基础设施适配器测试
```

模块采用 `router.py`、`service.py` 和按需拆分的 `schemas.py`；目录型 DTO 和筛选逻辑复用公共模块。样例 JSON 按领域存放，读取时复制。场景应用由 `application_service.py` 处理业务规则、`memory_repository.py` 在每个 worker 内暂存新增状态，`ports.py` 定义后续数据库/供应方替换合同。该模块的发布审核状态尚未跨进程共享，完整流程演示需使用 `-Workers 1`，重启后重置；正式业务需接入持久化仓储。认证、身份同步、看板及对话历史已通过数据库在 worker 间共享。

## 本地开发

推荐 Python 3.12 或 3.13，当前使用 3.13。运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

默认 `APP_ENV=development`、`AUTH_MODE=disabled`。看板和会话需要配置数据库并执行迁移；其余匿名 Mock 接口不依赖外部服务。启用 `AUTH_MODE=mock` 后，看板、会话和其他业务接口都要求有效登录会话，写请求校验 CSRF；需要执行完整迁移并单独运行模拟 IAM。

所有本地配置统一放在项目根目录 `.env`，包括 `POSTGRES_*`、`REDIS_*`、`MINIO_*` 和 `AUTH_*`；数据库和 Redis 支持分项账号密码，兼容显式连接 URL，显式 URL 优先。`.env.example` 是可提交的配置模板，真实密码仅放入忽略的 `.env` 或环境变量。用户端和管理端的 Vite 默认将 `/api` 代理到端口 8000。

集团真实 CAS 签名和接入信息尚未提供，因此没有生产认证模式，模拟 IAM 在 production 环境禁止启用。配置 PostgreSQL、执行迁移并构建两套前端后，可使用以下脚本启动认证联调；生成器统一更新根目录 `.env`，保留已有服务凭据和联调密钥。

```powershell
.\.venv\Scripts\python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
.\scripts\start-local.ps1 -NginxRoot <已解压的Nginx目录> -ApiPort 18000 -Workers 2 -GatewayPort 18880 -MockIamPort 18002
```

用户端入口为 `http://127.0.0.1:18880/`，管理端为 `/admin/`。API 禁用带查询串的访问日志，避免记录 CAS 票据；停止服务使用 `scripts/stop-local.ps1 -ProcessRecord <启动输出的记录路径>`。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/docs` | 开发接口文档，Swagger 资源使用上游 CDN |
| `GET /api/v1/framework` | Mock 模式、当前实例及模块清单 |
| `GET /api/v1/health/live` | 不依赖外部服务的存活检查 |
| `GET /api/v1/health/ready` | 数据库连接检查；未配置时返回 503，不代表业务表已迁移 |
| `GET /api/v1/health/dependencies` | 开发/测试环境的真实服务连接状态 |

Mock 接口只注册在 development/test 环境；数据库看板和会话在 production 返回 503，样例业务路由及依赖诊断不开放。production 要求配置数据库，当前没有正式业务 API。启动不会建表、建桶或调用供应方。

## HTTP Mock 契约

默认前缀 `/api/v1`，JSON 字段使用 camelCase；响应带 `X-Request-ID`、`X-App-Instance`，开发/测试环境另带 `X-App-Worker` 便于验证进程间会话共享。样例业务接口带 `X-Data-Mode: mock`，数据库看板与会话使用 `X-Data-Mode: development`。认证开关控制会话及 CSRF 校验；认证接口不标记为固定样例数据。

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

看板 PUT 在完成登录及 CSRF 校验后仍返回 503 `overview_publication_not_configured`，接入可信发布方后再开放。会话数据持久化，仍为共享开发历史，尚未实现用户数据隔离。

场景应用的专用发布/审核接口在当前实例内保存跨请求状态，浏览器刷新仍可查询，服务重启后重置。其他模块仍为固定回执，例如用户端反馈不会新增到管理端。文件只传文件名、大小等元信息，不上传二进制；会话兼容入口接收但不保存附件。场景应用在 HTTP 模式下按账号保存收藏、订阅和评分；其他目录收藏仍是本机演示状态。后续关联表设计见 `database/scenario-applications/schema.sql`。

这套接口用于验证传参、响应、错误、加载状态和组件交互。后续开发真实业务时再定义领域模型、身份权限、事务、文件状态和供应方适配，不把 Mock 回执当作实际业务验收。

## 基础设施

| 能力 | 项目已有内容 | 当前边界 |
| --- | --- | --- |
| PostgreSQL | 单一连接池入口、请求级会话、显式事务、本机持久化提交 | 看板、会话、认证与身份同步持久化；其余 Mock 业务不持久化 |
| Redis | 共享客户端、环境前缀、TTL、失败按未命中处理、Compose 模板 | 开发环境可配置真实服务；尚未实现完整业务缓存 |
| MinIO | 对象上传、读取、删除、大小检查、连接回收 | 基础适配器及服务探测；没有业务文件 API |
| Web 入口 | 各一份前端静态资源、单个 API 端口与多个 worker | worker 数量按 CPU、连接池和压测结果确定；不自动重试代理请求 |
| 备份 | 数据库逻辑备份和隔离恢复；指定对象前缀备份及新桶恢复 | 不依赖已移除的业务表；定时独立备份尚未部署 |

所有读写使用 `SessionDependency`，业务服务显式提交。每个 worker 独立建立连接池，数据库最大应用连接数为 `workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`，另外预留迁移、运维和备份连接。2 万在线用户不对应 2 万条数据库连接，具体容量需按请求类型、耗时与缓存命中率压测。

应用、迁移及恢复连接使用 `synchronous_commit=local`，等待本机 WAL 落盘后确认提交。显式写事务使用 `DB_STATEMENT_TIMEOUT_MS` 限制语句耗时，锁等待上限为 3 秒；提交遇到断连仍可能返回 `commit_outcome_unknown`，不能直接认定已回滚或盲目重试。身份同步已按事件编号处理幂等，其他正式业务按领域补齐。

`CacheDependency` 未配置时返回 `None`，传输失败按缓存未命中处理；`StorageDependency` 未配置返回 503。适配器均为同步实现，应使用同步路由或工作线程。健康检查会实际访问服务；数据库看板、会话和认证也访问数据库，其余页面样例不会。

## 数据库迁移

数据库可使用 `POSTGRES_HOST/PORT/DB/USER/PASSWORD` 分项配置，兼容 `postgresql+psycopg://` 形式的 `DATABASE_URL`。可选 `POSTGRES_MIGRATION_*` 或 `DATABASE_MIGRATION_URL` 指定同一个数据库的迁移账号；每次发布单独执行一次迁移，不在各 worker 启动时运行。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe schema change"
```

迁移链为 `20260913_01`（空基线）→ `20260914_01`（overview_snapshots）→ `20260914_02`（demo_conversations）→ `20260914_03`（认证三表及旧分支兼容）→ `20260915_01`（本地历史版本兼容标记）。

最早两个分支曾用同一个 `20260914_01` 分别创建看板表和认证三表，`20260914_03` 会补齐缺表并保留已有数据。之后本地与远端分别用 `20260915_01` 和 `20260914_03` 建立了相同结构，因此保留两者编号，实际建表统一由 `20260914_03` 完成，`20260915_01` 只保留版本标记。已有任一版本的数据库执行 `upgrade head` 即可，无需手工 stamp 或删表；从 `20260915_01` 回退到 `20260914_03` 不修改表，继续回退到 `20260914_02` 才删除认证三表。

认证三表为 `auth_session`、`auth_login_request`、`auth_event_log`。自动生成迁移会跳过仅在数据库中反射出来、未在当前模型中登记的表，避免误删历史测试表；确需删除表时手工编写并审核迁移。

`20260915_02` 在此之后增加身份状态、角色授权和同步回执三表，并为会话的用户标识增加索引。回退该版本不删除已有登录会话或业务数据。

`20260915_03` 补齐部分历史库遗漏的 `demo_conversations` 表，不修改已有会话、认证或身份同步数据。历史迁移编号继续保留，保证已有数据库可直接升级。

## 单服务器部署

根目录 `.env.example` 是唯一配置模板。复制为忽略的 `.env` 后填写服务密码；本机部署时 `POSTGRES_HOST`、`REDIS_HOST` 均使用 `127.0.0.1`，`REDIS_USER=default`，MinIO Endpoint 对应本机 API 端口。`compose.yaml` 启动 PostgreSQL 17、Redis 和 MinIO，数据使用各自的持久卷，端口默认仅监听本机：

```powershell
docker compose config --quiet
docker compose up -d
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.verify_services --write-probes
```

MinIO 的 `MINIO_ROOT_*` 是容器管理账号；在控制台创建 `MINIO_BUCKET` 指定的 Bucket，并配置对应 `MINIO_ACCESS_KEY/SECRET_KEY` 应用凭据。已配置有建桶权限的账号时，也可显式运行 `verify_services --create-bucket`。已有开发服务直接填写连接信息即可，无需再次运行 Compose。

用户端在 `AiCollabHub_User` 中执行 `npm ci`、`npm run build`；管理端在 `AiCollabHub_Admin` 中设置 `VITE_BASE=/admin/`、`VITE_USER_URL=/`、`VITE_API_BASE_URL=/api/v1` 后执行相同构建命令。每个项目仅部署一份 `dist`。本地使用前述 `start-local.ps1`；Linux 服务器运行 `uvicorn app.main:app --host 127.0.0.1 --port 18000 --workers <进程数> --no-access-log`，由 systemd 等进程管理器托管。

Nginx 配置可用 `python -m scripts.render_nginx --nginx-root <目录> --user-dist <用户端dist> --admin-dist <管理端dist> --api-port 18000 --port 18880 --listen-host 0.0.0.0` 生成，执行 `nginx -t` 后加载。生成器在 Windows 默认使用一个 Nginx 进程，Linux 默认按 CPU 自动设置进程数，也可用 `--nginx-workers` 显式指定。每个进程允许 8192 个连接；代理请求还占用上游连接，Linux 的文件描述符限制需配套调整并压测。HTTPS 域名和证书按交付环境补充，启用 HTTPS 时同步调整认证公开地址及 Cookie 配置。该模板不包含正式证书和服务器启动托管设置。

数据库备份使用 `python -m scripts.backup_database --directory <备份目录> --pg-bin <PostgreSQL工具目录>`，工具版本需兼容数据库服务器；加 `--restore-check` 会创建全新的恢复验证库并核对各表行数，需要建库权限。MinIO 备份使用 `scripts/backup_objects.py`，对象恢复到新的 Bucket。备份计划、保留期限和机外副本由部署时配置，单服务器本机副本无法覆盖整机损坏。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
node scripts/verify_framework_browser.mjs
# AUTH_MODE=mock 的网关使用以下登录验证脚本：
node scripts/verify_auth_browser.mjs
```

Python 测试覆盖单库配置、提交异常、缓存和对象客户端、各模块 Mock 契约，以及模拟 CAS、共享会话、单点登出、CSRF、迁移。匿名 Mock 契约测试禁止外部服务调用，看板、会话和认证使用独立临时库，迁移测试覆盖历史分支升级、缺表修复及数据保留。浏览器脚本依赖用户端已安装的 Playwright、Chrome 及已启动的网关；框架脚本用于认证关闭的环境，认证脚本验证桌面/手机登录、同页切换、管理端准入、Mock 写请求和同端口多 worker 的会话及登出一致性。可用 `FRAMEWORK_URL`、`API_URL`、`EXPECTED_API_WORKERS` 指定入口和预期 worker 数量。

`scripts.verify_services --write-probes` 实测数据库、Redis 和 MinIO 的写入、读取及删除，并核对 ORM 所需表是否存在；探测使用随机测试数据，结束后清理。框架测试通过不代表已完成正式业务、物理服务器部署或 2 万在线/5000 并发容量验收。

## 最小身份同步

仅在 `AUTH_MODE=mock` 的开发/测试环境开放 `POST /api/v1/auth/mock/identity-sync`。使用后端的 `X-Mock-IAM-Key` 验证模拟来源，普通用户或管理员的浏览器 Cookie 都不能代替此密钥。接口采用平台内部事件格式，不能直接接收集团报文。

| 表 | 保存内容 |
| --- | --- |
| `iam_identity_state` | 与 CAS 身份一致的 `subject`、账号状态、用户状态源版本和更新时间 |
| `iam_identity_role` | `subject`、平台角色码、授权或撤权状态、该角色的源版本 |
| `iam_sync_receipt` | 事件编号、内容摘要、处理数量、重试次数和接收时间，不保存完整推送原文 |

没有姓名、手机号、工号映射、完整人员档案或组织树。角色首次推送前沿用模拟管理员名单；一旦收到该角色的授权或撤权记录，数据库结果优先。用户状态尚未推送时，允许已通过模拟 IAM 验票的账号登录，此兼容规则不适用于尚未实现的生产认证。

请求包含 `eventId`、`appId=platform`、`resType`、`action`、`syncTime` 和最多 100 条 `data`，报文上限 64 KiB。用户事件使用 `resType=user`，每项为 `subject` 与 `status=active/disabled/deleted`；角色事件使用 `resType=userlinkedrole`，每项为 `subject`、内部能力码 `roleCode=platform_admin` 和 `status=authorized/unauthorized`。`action=recycle` 强制停用账号或撤销该角色，不受报文中原状态影响。`syncTime` 是源版本，模拟发送器默认使用毫秒时间戳；用户状态和角色各自比较版本，不用请求到达时间排序。

相同 `eventId` 和内容重试时返回已有回执并标记 `duplicate=true`，统计数量仍指原事件；更换内容返回 409 `sync_event_conflict`。旧版本计入 `ignored`；相同版本且状态相同计入 `unchanged`，状态冲突返回 409 `sync_version_conflict`。批次内任一冲突会整体回滚，状态、会话撤销和回执不会部分生效。未知用户的停用或撤权也保留状态记录，防止迟到的旧推送重新启用。

停用、注销或回收用户会撤销该用户全部平台会话，并阻止再次登录；之后重新启用必须重新登录，旧会话不会恢复。撤销管理员角色立即限制后续管理端请求，保留用户端会话；两端通过现有会话检查更新界面。登录回调和同步推送锁定同一身份行，避免停用之后迟到的回调重新建立会话。已在停用提交前通过权限检查的在途业务请求不在本轮强制中止范围内。

成功响应在单库事务提交后返回。若收到 503 `commit_outcome_unknown` 或连接中断，使用同一事件文件重试；重试按已持久化的事件回执去重。`GET /api/v1/auth/mock/identity-sync/{eventId}` 使用同一来源密钥查询回执，可用于确认处理结果。

本地模拟推送命令会先将不含密钥的事件保存至忽略的 `.runtime/identity-sync/`，再使用根目录 `.env` 的配置发送：

```powershell
.\.venv\Scripts\python.exe -m scripts.mock_identity_sync --subject mock-user --status disabled
.\.venv\Scripts\python.exe -m scripts.mock_identity_sync --subject mock-user --status active
.\.venv\Scripts\python.exe -m scripts.mock_identity_sync --subject mock-admin --resource userlinkedrole --status unauthorized
.\.venv\Scripts\python.exe -m scripts.mock_identity_sync --subject mock-admin --resource userlinkedrole --status authorized
# 失败重试必须使用原事件文件，不重新生成版本和事件编号：
.\.venv\Scripts\python.exe -m scripts.mock_identity_sync --retry <上次输出的事件文件路径>
```

集团 `CAS user/id`、`workNo`、`userNo` 的对应关系、状态值含义、角色编码及组织任职范围尚待确认；特别是文档中 `3000=停用` 与 `>=2000=有效` 的冲突不在本轮猜测处理。组织/字典全量同步、集团签名、真实 IAM 接收适配和同步管理页面未实现。身份状态、撤权记录与同步回执暂不自动清理，不能随会话过期删除后回退到模拟默认权限。

`tests/test_identity_sync.py` 覆盖模拟接口与事务；`tests/test_identity_sync_postgresql.py` 仅在显式提供 `IDENTITY_SYNC_TEST_DATABASE_URL` 时运行，使用可写 PostgreSQL，在随机独立 Schema 中验证重复推送、乱序、批次回滚，以及登录与停用的双向并发顺序，完成后清理该 Schema。

本地网关的浏览器联调可设置 `VERIFY_IDENTITY_SYNC=1` 后运行 `node scripts/verify_auth_browser.mjs`，在原有登录验证上增加管理员撤权、旧推送拦截、双端停用和重新登录检查。该模式会临时变更模拟管理员的状态，验证结束后恢复可用；密钥只在验证进程使用。

## 数据库看板与多轮会话

首次合并后安装 `python -m pip install -e ".[dev]" -c requirements-dev.lock`，以补齐
FastAPI 标准依赖与 Redis、MinIO、PostgreSQL 驱动；然后配置数据库并执行迁移。
也可使用 `fastapi dev`，入口由 `pyproject.toml` 声明。

会话为共享开发记录，按配置校验登录，但尚无用户数据隔离。`create_app(chat_provider=...)` 可注入
第三方回复实现，默认回显；完整消息通过 `/conversations/{id}/messages` 读取。
附件仅由旧 multipart 提交入口兼容接收，不保存或参与回答；多轮 JSON 不支持附件。
数据库未配置、未迁移或不可用均返回错误，不回退成无状态 Mock。
