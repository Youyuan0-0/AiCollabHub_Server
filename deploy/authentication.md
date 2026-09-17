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

会话内部关联 `users.id` UUID；本次保持现有 `/auth/session` 的 `user.id` 及业务归属字段为 IAM subject，避免改变对话、收藏和发布记录的归属契约。场景应用的显示名从 `users` 读取。后续业务 UUID 外键迁移须独立核对历史归属。

## 升级与恢复

迁移 `20260917_auth01` 基于反馈模块迁移 `20260917_01`，删除旧认证/同步六表。执行前核对数据库版本属于完整的本地迁移链；如果数据库标记了仓库没有的版本，先补齐对应迁移，不能 stamp 回退或跳过。此迁移必须在线执行。

1. 暂停旧后端全部 worker 和模拟同步发送器，避免旧服务在迁移后继续访问已删除的表。
2. 使用与服务端兼容的 `pg_dump` 完整备份并验证恢复；PostgreSQL 17 不能使用 16 的 pg_dump。
3. 配好 Redis，使用私有备份目录执行迁移。示例管理员名单仅适用于 Mock，须与实际配置一致；数据库已记录的撤权优先于名单。

```powershell
.\.venv\Scripts\python.exe -m alembic -x auth_archive_dir=.runtime/auth-backup -x auth_mock_admin_subjects=mock-admin upgrade head
.\.venv\Scripts\python.exe -m alembic check
```

迁移先锁住旧六表，归档并校验完整 JSON 快照，再迁移用户、共享会话和成功日志；没有指定归档目录的非空库拒绝升级。归档目录应限制访问并复制至备份存储，不提交仓库。它用于核对旧回执和失败日志，不代替完整数据库备份。

普通共享会话保留原令牌/票据摘要、到期和撤销状态；已停用用户与旧分端会话失效。旧 SQL 登录临时上下文不复制到 Redis，在途登录须重新发起。提前单点登出的票据标记保留。未知旧角色映射或无效旧会话会中止迁移，需先核对。

旧回执缺少业务原文且摘要包含 `eventId`，只进入归档，不冒充新摘要。模拟发送器同步切换为无 `eventId` 报文。跨版本重试保留原始业务内容和 `syncTime`，只移除旧事件编号；已应用的同版本内容保持不变，冲突报错，不重复写成功日志。

升级后启动新版后端，验证两端共用会话、停用、撤权、重复同步。`downgrade` 明确拒绝有损回退；需要回滚时停止新版写入，核对切换后的新增数据，再整体恢复升级前备份及配套旧代码，不能直接清空新表或 stamp 旧版本。

同一个测试库的其他开发者也必须切换到包含四表迁移的后端代码。旧代码仍访问已删除的认证表，不能与新版共用升级后的数据库；前端接口的用户标识契约保持不变，无需为本次内部表结构变化改写用户编号。

## 本地联调

在根目录 `.env` 配置 PostgreSQL 和 Redis 后执行（非空旧库先按上节升级）：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
.\scripts\start-local.ps1 -NginxRoot <Nginx目录> -ApiPort 18000 -Workers 2 -GatewayPort 18880 -MockIamPort 18002
```

配置工具更新根目录 `.env`，保留已有服务配置及联调密钥。API 与模拟 IAM 使用相同配置。两端前端需先按 [部署说明](README.md) 构建；场景应用使用内存存储时改用 `-Workers 1`。

## 接口与验证

| 接口 | 职责 |
| --- | --- |
| `/api/v1/auth/config` | 返回认证开关 |
| `/api/v1/auth/login`、`/callback` | 发起登录、校验上下文及票据 |
| `/api/v1/auth/session` | 查询会话、CSRF 值及管理端准入 |
| `/api/v1/auth/logout`、`/single-logout` | 主动退出及单点登出 |
| `/api/v1/auth/mock/identity-sync` | 接收内部格式的模拟账号和角色事件 |

身份同步使用 `X-Mock-IAM-Key` 校验来源，按规范化业务报文摘要去重、按来源时间处理乱序。回执、用户更新、会话撤销和成功日志在同一事务提交。停用账号会撤销其会话，撤销管理员授权会限制后续管理端请求。报文与重试规则见 [最小身份同步](../README.md#最小身份同步)。

隔离测试执行 `python -m pytest`。已启动模拟登录网关时，可运行 `node scripts/verify_auth_browser.mjs`；它会操作模拟会话。带 `VERIFY_IDENTITY_SYNC=1` 的验证还会临时修改模拟身份状态并恢复。

模拟来源密钥只保存在本机配置；它不能替代正式 IAM 的签名与来源认证。日志避免记录 CAS 票据，直接启动 Uvicorn 时使用 `--no-access-log`。
