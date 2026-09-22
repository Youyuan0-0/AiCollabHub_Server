# AiCollabHub Server

人工智能协同平台的 FastAPI + SQLAlchemy 后端。平台业务已适配 `resource_foundation` 新表结构，开发联调使用 PostgreSQL 测试库；集团 IAM、模型调用、外部工具链及 Token/GPU 计量仍按各自配置使用模拟实现或保持关闭。

部署统一为单服务器：用户端和独立管理端各一份构建产物，一个 Nginx 入口，一套多 worker 后端，以及 PostgreSQL、Redis、MinIO 各一个实例。数据库全部读写访问同一实例，不维护主备及读写分离方案。

## 本地启动

推荐 Python 3.13。三端仓库按相邻目录放置，前端复用后端仓库 `shared/ui`：

```powershell
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
```

参考 `.env.example` 填写本地 `.env`：

- `APP_ENV=test`，`AUTH_MODE=mock`。
- `POSTGRES_HOST/PORT/DB/USER/PASSWORD` 指向已确认的新结构测试库；也支持优先级更高的 `DATABASE_URL`。
- `REDIS_*` 用于共享登录上下文和外部模拟运行态。
- `MINIO_*` 指向已创建的 Bucket；平台保存文件元信息，MinIO 保存原件和预览。
- `AUTH_*` 配置模拟 IAM、浏览器公开来源和共享密钥，参见 [认证说明](deploy/authentication.md)。

真实凭证只放在被忽略的 `.env` 或环境变量中。PostgreSQL 一旦配置，启动即装配新表仓储，不受旧 `APPLICATIONS_STORAGE/DATASETS_STORAGE/KNOWLEDGE_STORAGE=memory` 的影响。缺表或连接错误会明确失败，不自动建表、导入样例或降级为内存成功。无 PostgreSQL 的内存实现只供隔离契约测试，不是完整平台运行方式。

首次建立新结构时，先核对连接目标，再执行：

```powershell
.venv/Scripts/python.exe -m alembic -n resource_foundation current
.venv/Scripts/python.exe -m alembic -n resource_foundation upgrade head
.venv/Scripts/python.exe -m scripts.verify_services
```

这条迁移链只接管空 schema 或自身已有版本，不能直接转换旧业务库。当前共 43 张业务表，最新版本为 `20260921_04_knowledge_external`。默认 `alembic upgrade head`、旧独立会话迁移及 `database/scenario-applications` SQL 属于历史结构，不能对新库执行。切换数据库不是迁移旧记录；本次适配不复制旧库数据、不恢复暂存重构。详见 [新库基线](database/resource-foundation/README.md)。

完成用户端 `pnpm install --frozen-lockfile`、`pnpm build`，管理端 `npm ci`、`npm run build` 后，在后端启动：

```powershell
.venv/Scripts/python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
./scripts/start-local.ps1 -NginxRoot <已解压的Nginx目录> -ApiPort 18000 -Workers 2 -GatewayPort 18880 -MockIamPort 18002
```

用户端入口 `http://127.0.0.1:18880/`，管理端 `/admin/`。停止时执行 `scripts/stop-local.ps1 -ProcessRecord <启动输出的记录路径>`。修改后端或 `.env` 后需重启；前端使用 Nginx 托管构建产物时，修改源码后需重新构建。

单独调试后端可运行：

```powershell
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

接口文档为 `/docs`。`/api/v1/health/live` 检查进程，`health/ready` 检查数据库连接，`health/dependencies` 在开发环境检查数据库、Redis 和 MinIO。生产环境禁止模拟 IAM，不开放本阶段开发业务路由；切换新表不等于正式接口或容量验收。

## 数据与业务

| 功能 | 新表持久化内容 | 当前外部边界 |
| --- | --- | --- |
| 五类资源 | 各模块主表、发布申请、版本；共享审核、收藏、评分、关注、下载、范围和配置 | 外部目录、模型与智能体调用按供应方配置，默认模拟或关闭 |
| 知识库 | 概览与版本、文档原件、逐篇审核及历史 | 分段、向量、索引任务和召回统计不建平台 SQL 表，开发运行态在 Redis；真实工具链未完成联调 |
| 首页问答 | 用户会话、消息状态、发送幂等键、上下文和附件关系 | 默认回复回显问题，附件尚不参与模型回答 |
| 首页展示 | 资源统计、互动榜单、推荐、问题建议和公告 | 未配置内容返回空；Token/GPU 运营看板仍为 Mock |
| 问题反馈 | 反馈内容、回复和处理历史、附件关联；版本冲突和重复提交控制 | 图片及文档本体使用 MinIO |
| 身份与日志 | 用户、共享会话、身份同步回执、操作日志、系统日志 | 集团正式 CAS 签名、状态映射和推送协议仍待确认 |
| 个人中心 | 发布、收藏、关注和下载聚合；身份资料从用户表读取 | 联系信息不增加本地编辑存储 |

主表只保存当前正式资料。待审修改冻结在发布申请中；审核通过时在同一事务固化版本、审核历史及主表概览。首次通过审核仍默认禁用，管理员显式启用后进入公开目录。API 兼容 `listed/unlisted`，数据库保存 `enabled/disabled`。

开放范围为全部用户或指定成员，成员从 `users` 获取，不使用模拟单位或项目作为授权依据。API 身份 ID 仍为 IAM subject，写库时映射为 `users.id` UUID。版本、审核和历史文件引用不可原地覆盖。

会话绑定登录用户。发送前以短事务保存提问、附件和回答占位，事务外调用提供器，再保存结果；相同发送键复用原结果，失败状态保留。中断超过 15 分钟的占位在重新读取时标为失败；已删除会话不接收迟到回复。详见 [会话说明](deploy/conversations-postgresql.md)。

反馈按同一用户的 `clientRequestId` 防重复提交；管理员保存携带当前 `expectedVersion`，冲突返回 409，不覆盖他人的处理。反馈、事件及附件引用原子更新。

文件使用 `files`、`resource_file_references`、`file_previews` 和模块关联表登记。校验通过后才标为就绪，对象传输不长期占用事务；清理前检查引用并取得租约。历史版本、审核报告和文档原件受引用保护。数据库和 MinIO 须一起规划备份。

站内个人通知、外部资源映射和本地知识索引三表没有创建。本阶段不为这些能力新增隐含数据表。

## 源码组织

```text
app/
  core/                  配置、资源生命周期及新库服务装配
  db/                    连接池、事务、身份模型、新基线表访问
  api/                   路由注册、健康、首页与会话
  schemas/               首页与会话契约
  services/              首页聚合、会话仓储、附件与回复编排
  cache/                 Redis 适配
  storage/               MinIO、文件登记和清理
  modules/
    datasets/            数据集目录、发布、审核、样例下载
    models/              模型目录、发布、审核
    applications/        场景应用、教程、资源依赖、发布及体验
    knowledge/           知识库及文档管理、外部处理适配
    tools/               AI 工具发布、审核与运行
    feedback/            问题反馈和管理员处理
    profile/             个人资料与资源活动聚合
    common/              共用资源仓储、审核、权限、身份和工具链
    admin/               管理端概览与日志查询
migrations/resource_foundation/  当前新库迁移链
database/resource-foundation/    表结构说明
deploy/                          单服务器部署模板
scripts/                         启停、服务检查和备份
tests/                           契约、权限、迁移和业务持久化回归
```

共用仓储 `modules/common/resource_database.py` 对接五个模块的独立表，DTO 转换、互动及文件按职责分文件。`db/foundation.py` 复用连接池，启动反射现有表，不执行 DDL。历史迁移保留用于旧库审计和回归，不参与新库装配。

## 认证与日志

两端共用 HttpOnly `ach_session` Cookie，路径 `/api`。任一端登录后另一端沿用身份；管理接口逐次检查管理员角色，写请求验证 Origin 与 CSRF。退出、会话到期、单点登出或停用后，两端均失效。前端在路由切换、恢复焦点和定时检查时更新。

模拟身份同步接口只接受服务端来源密钥。用户与角色分别比较来源时间，重复报文复用回执，旧数据忽略，同时间冲突返回 409；用户、会话撤销、成功日志和回执同事务提交。角色推送不能激活账号，撤权立即限制后续管理请求。正式组织、字典及集团报文适配未完成。五个模拟账号和联调命令见 [认证说明](deploy/authentication.md)。

操作日志记录成功业务动作；异常、权限拒绝和运行事件进入系统日志。系统日志后台批量写 `system_logs`，级别 INFO/WARNING/ERROR，入库前脱敏；队列满或写库失败时降级到控制台，不改变业务响应。内存队列不能保证进程强制终止时零丢失。

管理端日志页面读取真实表，校验管理员身份，支持筛选、排序和分页。系统详情保留请求编号和受控上下文；页面不会为尚无埋点的业务补日志。

## 连接与部署

数据库单池、每个 worker 独立，应用上限为 `workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`。开发默认每 worker 5 条常驻、2 条溢出，两个 worker 最多 14 条，另留迁移和运维额度。池占满后按 `DB_POOL_TIMEOUT` 等待，不能把在线人数等同于连接数。

应用 PostgreSQL 连接使用 `synchronous_commit=local`，等待本机 WAL 持久化。提交断连可能返回结果不确定，不能盲目重试；业务使用幂等键核对。`DB_IDLE_SESSION_TIMEOUT=120` 只作用于本项目新连接，借用失效连接时重连，不重放业务事务。

`scripts.verify_services` 默认只读检查 43 张表、数据库、Redis 和 MinIO；`--write-probes` 创建随机探测对象并清理。`scripts.verify_database_pool` 检查池复用和空闲回收，不改服务器全局参数。

[单服务器部署](deploy/README.md) 说明构建、容器环境和网关。迁移每次发布只运行一次，不随 worker 重复运行。数据库备份使用 `scripts.backup_database`，MinIO 使用 `scripts.backup_objects`，恢复验证使用独立目标。定时备份、机外副本、HTTPS 和容量测试由部署阶段落实。

## 导入原型样例

原先的静态目录共 54 条资源，可以显式导入新表作为开发测试资料。脚本复用发布、审核、版本及可见范围仓储，首次导入时模拟审核并启用；固定 UUID 保证重跑只跳过，不覆盖修改、下架状态或互动数据。整批写入使用一个事务，失败全部回滚。

```powershell
.venv/Scripts/python.exe -m scripts.seed_demo_resources --database aicollabhub_test
.venv/Scripts/python.exe -m scripts.seed_demo_resources --database aicollabhub_test --apply
```

省略 `--apply` 时仅预览。目标必须与 `.env` 一致且属于 development/test 环境，执行前备份测试库；默认发布者为已登录入库的 `mock-user`，审核者为 `mock-admin`，可通过 `--publisher`、`--reviewer` 指定现有身份。脚本不创建用户、不提升权限，也不随应用启动自动执行。

名称、概览、标签、字段与公开样例、应用教程及已知依赖会保存到对应表。仅生成资料完整的当前版本，Mock 来源写在申请及版本说明中。预设评分、收藏、下载和调用数不导入；知识库样例没有文件本体，因此不伪造文档、分段或文件记录。非公开知识库先设为发布者可见，可由管理员调整范围。外部 IAM、模型和工具调用仍为模拟。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check app scripts tests migrations
.venv/Scripts/python.exe -m ruff format --check app scripts tests migrations
```

默认测试不连接远端。PostgreSQL 回归须显式配置 `RESOURCE_FOUNDATION_TEST_DATABASE_URL`，指向允许创建 schema 的独立测试库，然后运行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_resource_foundation.py tests/test_resource_support.py tests/test_home_knowledge_schema.py tests/test_knowledge_scope_migration.py tests/test_foundation_resource_storage.py tests/test_foundation_module_flows.py tests/test_foundation_models.py tests/test_foundation_home_feedback.py tests/test_home_database.py tests/test_feedback.py -q
```

这些测试在随机 schema 和外层事务中运行，不修改 public 记录；页面联调另用明确标记的测试资料。前端分别执行构建、已有测试和改动文件格式检查。功能回归不等于 2 万在线、5000 并发容量验收。

代码约定见 [项目代码规范](docs/AiCollabHub-代码规范.md)、[资源主表设计](docs/AiCollabHub-资源主表设计.md) 和 [场景应用源码说明](app/modules/applications/README.md)。
