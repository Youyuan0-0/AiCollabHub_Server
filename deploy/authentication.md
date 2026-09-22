# 共享登录与身份同步

认证模块位于 `app/modules/common/auth/`，最小身份同步位于 `app/modules/common/identity_sync/`。当前支持开发环境中的模拟 IAM，正式集团 CAS、组织档案和完整角色映射仍需接入。

## 模拟账号

| 登录页名称 | IAM 标识 | 初始角色 |
| --- | --- | --- |
| 模拟用户A | `mock-user` | `user` |
| 模拟用户B | `mock-user-b` | `user` |
| 模拟用户C | `mock-user-c` | `user` |
| 模拟管理员A | `mock-admin` | `admin` |
| 模拟管理员B | `mock-admin-b` | `admin` |

A 账号沿用原有标识，历史记录归属保持不变。新增账号首次成功登录时按需建档，无需新增数据表。`AUTH_MOCK_ADMIN_SUBJECTS` 默认包含两个管理员；已有环境若显式配置旧名单，需同步更新为 `["mock-admin","mock-admin-b"]`，并重启 API 与模拟 IAM。身份同步已记录的停用或撤权仍优先，不能通过重新登录恢复权限。

## 会话规则

用户端和管理端共用 `ach_session` Cookie，路径为 `/api`，启用 HttpOnly 和 SameSite=Lax；HTTPS 环境需同时启用 Secure。管理端在后端逐次校验管理员准入，写请求校验 Origin 和 CSRF。

任一端退出、会话失效或收到单点登出通知后，两端共用的身份失效。只接受共享 Cookie；旧 `ach_session_user/admin` 不再用于登录，登录或退出时清除。认证和身份同步的所有数据库操作使用单库连接。

| 表 | 职责 |
| --- | --- |
| `users` | UUID、唯一 IAM 标识、姓名、状态、角色、两类同步时间及本地创建/更新时间 |
| `auth_sessions` | 关联用户的共享会话、令牌/票据摘要、期限与撤销状态；也保存提前登出标记 |
| `operation_logs` | 成功操作的发起方、动作、对象、追踪编号和受控摘要；失败进入系统日志 |
| `iam_sync_receipts` | 已提交批次的规范化报文摘要、资源、动作、来源时间与建档时间 |

用户状态为 `pending/active/disabled/deleted`，角色为 `user/admin`。角色推送不能激活账号；用户状态与角色各自比较 IAM `syncTime`。新增用户默认按需建档，平台不提供资料编辑入口。

登录临时上下文存 Redis，按环境隔离并设置 TTL，Lua 同时校验浏览器绑定、入口及一次性消费。Redis 故障时新登录返回 503，已有数据库会话继续校验，不能退回进程内状态或跳过绑定。

会话及业务表内部关联 `users.id` UUID；现有 `/auth/session` 的 `user.id` 和前端身份字段保持 IAM subject，写库时由新仓储映射为 UUID。显示名从 `users` 读取，不由浏览器自报。

## 升级与恢复

当前开发库使用 43 表新基线，认证四表及系统日志包含在其中。先核对目标数据库、使用兼容版本的 `pg_dump` 备份，再执行当前迁移入口；缺失版本不能通过 stamp 跳过。

```powershell
.\.venv\Scripts\python.exe -m alembic -n resource_foundation current
.\.venv\Scripts\python.exe -m alembic -n resource_foundation upgrade head
```

新基线仅支持空 schema 或同一迁移链，不能在旧业务库混用。历史 `20260917_auth01` 等迁移保留用于旧库审计，本环境不执行旧链。此次切换没有复制旧用户或会话，模拟用户首次登录按需建档。

启动新版后端后验证两端共享会话、停用、撤权和重复同步。回退需要配套旧代码和已核对的备份；同一测试库的所有开发实例均须使用兼容新基线的代码。

## 本地联调

在根目录 `.env` 配置新基线 PostgreSQL 和 Redis 后执行：

```powershell
.\.venv\Scripts\python.exe -m alembic -n resource_foundation upgrade head
.\.venv\Scripts\python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
.\scripts\start-local.ps1 -NginxRoot <Nginx目录> -ApiPort 18000 -Workers 2 -GatewayPort 18880 -MockIamPort 18002
```

配置工具更新根目录 `.env`，保留已有服务配置及联调密钥。API 与模拟 IAM 共用认证设置。两端前端需先按 [部署说明](README.md) 构建，平台资料由数据库跨 worker 共享。

## 接口与验证

| 接口 | 职责 |
| --- | --- |
| `/api/v1/auth/config` | 返回认证开关 |
| `/api/v1/auth/login`、`/callback` | 发起登录、校验上下文及票据 |
| `/api/v1/auth/session` | 查询会话、CSRF 值及管理端准入 |
| `/api/v1/auth/logout`、`/single-logout` | 主动退出及单点登出 |
| `/api/v1/auth/mock/identity-sync` | 接收内部格式的模拟账号和角色事件 |

身份同步使用 `X-Mock-IAM-Key` 校验来源，按规范化业务报文摘要去重、按来源时间处理乱序。回执、用户更新、会话撤销和成功日志在同一事务提交。停用账号撤销会话，撤销管理员授权限制后续管理请求。用户与角色分别比较 `syncTime`；旧时间忽略，同时间不同内容返回 409，批次整体回滚。

模拟报文为 `appId=platform`、`resType=user/userlinkedrole`、`action`、`syncTime` 和最多 100 条 `data`。用户事件字段为 `subject/status/displayName`，角色事件为 `subject/roleCode/status`；内部能力码 `platform_admin` 不冒充集团正式角色。报文不含 `eventId`，重试必须复用原内容和 `syncTime`，不能生成新来源版本。模拟来源时间使用毫秒，正式单位仍待确认。

本地可运行 `python -m scripts.mock_identity_sync --subject mock-user --status disabled`；恢复使用 `--status active`，失败重试使用 `--retry <事件文件>`。事件文件在被忽略的 `.runtime/identity-sync/`，密钥只从本机配置读取。

隔离测试执行 `python -m pytest`。已启动模拟登录网关时，可运行 `node scripts/verify_auth_browser.mjs`；它会操作模拟会话。带 `VERIFY_IDENTITY_SYNC=1` 的验证还会临时修改模拟身份状态并恢复。

模拟来源密钥只保存在本机配置；它不能替代正式 IAM 的签名与来源认证。日志避免记录 CAS 票据，直接启动 Uvicorn 时使用 `--no-access-log`。
