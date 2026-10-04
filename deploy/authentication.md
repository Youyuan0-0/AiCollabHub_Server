# 单点登录与本地角色管理

认证模块位于 `app/modules/common/auth/`。本期只接单点登录：未登录自动跳转 IAM，验票成功按 `workNo` 查找或创建本地用户，管理员通过服务器运维命令指定。`AUTH_IDENTITY_SYNC_ENABLED=false` 为默认配置，不挂载正式或模拟身份推送接口，不需要开放 8085。已有身份同步实现保留为后续显式启用的能力。本次不新增表，也不批量修改已有用户角色。正式适配尚未在航发网环境完成真实联调。

## 集团 IAM 接入

原生离线包的现场网关拓扑与下文早期直连方案不同：浏览器使用 `https://fdj-smartweb.aecc.cn`，统一网关终止 HTTPS 后转发到平台 HTTP 8086。原生包的 80/8086 均提供完整网站，`AUTH_PUBLIC_ORIGIN` 和 `AUTH_CALLBACK_ORIGIN` 都使用外部 HTTPS origin，`AUTH_COOKIE_SECURE=true`。部署以 `deploy/native/README.md` 为准；下文独立回调端口及 Windows 生成命令只适用于对应直连部署。

原生离线包自 `20261001-native-r5` 起默认启用集团 IAM，安装及升级自动生成并保存独立会话密钥，原模拟部署也会切换。现有正式 IAM 配置和密钥继续复用。开发启动及云端模拟环境仍需按各自配置选择认证模式。

2026-09-30 已确认：IAM 使用 HTTP 80，验票无应用层签名；CAS `attributes.workNo` 是集团用户稳定编号。本期 IAM 负责身份认证，平台 `users.role` 负责管理权限。单点登出通知暂不接入，平台主动退出使用 IAM `/logout`。`platform_admin` 是将来接入身份同步时的角色编码，本期不需要 IAM 配置或推送该角色。

| 配置项 | 已确认值 |
| --- | --- |
| IAM 来源 | `http://hfidaas-sso.aecc.cn` |
| AppID | `796872537136820224` |
| CAS 地址前缀 | `/authn-api/v5/cas/796872537136820224` |
| 登录 | 前缀 + `/login` |
| 验票 | 前缀 + `/p3/serviceValidate` |
| 主动退出 IAM | 前缀 + `/logout` |
| 后续身份同步角色编码 | `platform_admin`（本期不使用） |

早期直连方案的平台 IP 为 `20.1.4.240`，域名为 `fdj-smartweb.aecc.cn`，网站使用 HTTP 80。当前现场采用上述 HTTPS 网关方案，域名解析到网关，平台 IP 是网关上游。两种方案均应统一使用同一域名，不要交替使用 IP 和域名发起登录，否则浏览器无法复用同一主机的 Cookie。

登录链接只在显式重新认证时带 `renew=true`，普通登录省略此参数。验票发送 `ticket`、原始 `service`、`format=json`，兼容文档 XML 响应；仅使用 `attributes.workNo` 关联用户，不从顶层 `user` 或验票中的角色推断权限。HTTP 异常、超时、超大响应、缺少用户编号及非法报文都会拒绝建立会话，一次性票据不自动重试。

浏览器沿 IAM 重定向经 8086 返回本平台。8086 是入站 Nginx 监听，无需再运行一套 FastAPI。部署时登记以下确切路径：

| 方向 | 地址示意 | 用途 |
| --- | --- | --- |
| 平台/浏览器 → IAM | `http://hfidaas-sso.aecc.cn/authn-api/v5/cas/796872537136820224/...` | 登录跳转、服务端验票、主动退出 IAM |
| 浏览器 → 平台 8086 | `http://fdj-smartweb.aecc.cn:8086/api/v1/auth/callback` | 返回带 ticket 的 CAS 登录回调 |

早期直连部署使用以下地址，不能用于覆盖当前网关部署配置：

| 用途 | 地址 |
| --- | --- |
| 用户端 | `http://fdj-smartweb.aecc.cn/` |
| 管理端 | `http://fdj-smartweb.aecc.cn/admin/` |
| CAS service 回调基址 | `http://fdj-smartweb.aecc.cn:8086/api/v1/auth/callback` |

报备端口中的“单点登录服务访问平台 8086”需要补充用户终端来源：CAS 登录由 IAM 返回重定向，实际向 8086 发请求的是用户浏览器，只允许 IAM 服务器 IP 无法完成回调。用户终端还需能访问 IAM HTTP 80 登录页面，后端也需能访问 IAM HTTP 80 验票接口。本期未接收 IAM 单点登出通知。

单服务器正式部署的端口保持原报备分工：

| 端口 | 当前用途与访问范围 |
| --- | --- |
| 80 | 用户端、管理端统一网站入口，允许授权用户终端访问 |
| 443 | 仅在后续启用 HTTPS 时配置 |
| 8086 | CAS 浏览器回调入口，允许授权用户终端访问；仅代理回调精确路径 |
| 8085 | 本期不启用身份同步，不监听；后续启用时仅允许实际 IAM 推送来源 |
| 5432、6379、9000 | PostgreSQL、Redis、MinIO API，仅本机后端访问 |
| 9001 | MinIO 控制台，仅本机运维访问 |
| 18000 | 后端 API，仅本机 Nginx 转发，不对外开放 |

前端站点和 8086 回调必须使用**同一主机名和协议**（端口可以不同），浏览器才能携带绑定的临时登录 Cookie，并在回调后复用共享会话。IAM 登记的 `service` 要允许当前 `/callback?state=...&scope=...` 参数，验证票据时原样传回。回调成功和失败均跳回 `AUTH_PUBLIC_ORIGIN`，不在 8086 加载前端页面。8086 只代理回调精确路径，其余路径 404；网站端口不转发 `/api/v1/auth/iam/`。示例生成参数：

```powershell
.\.venv\Scripts\python.exe -m scripts.render_nginx --nginx-root <Nginx根目录> --user-dist <用户端dist> --admin-dist <管理端dist> --listen-host 0.0.0.0 --port 80 --api-port 18000 --iam-callback-port 8086
```

内网部署时在现有 `.env` 中设置以下参数：

```dotenv
AUTH_MODE=iam
AUTH_IDENTITY_SYNC_ENABLED=false
AUTH_PUBLIC_ORIGIN=http://fdj-smartweb.aecc.cn
AUTH_CALLBACK_ORIGIN=http://fdj-smartweb.aecc.cn:8086
AUTH_IAM_ORIGIN=http://hfidaas-sso.aecc.cn
AUTH_IAM_APP_ID=796872537136820224
AUTH_IAM_TIMEOUT=5
AUTH_SESSION_SECRET=<至少32字符的独立随机密钥>
AUTH_COOKIE_SECURE=false
```

配置完整后 `create_app()` 会自动装配并关闭正式客户端，不需要再注入验签器。保留现有 PostgreSQL、Redis 配置；所有 worker 使用相同的会话密钥和共享 Redis。HTTPS 站点需同时使用 HTTPS 回调及 `AUTH_COOKIE_SECURE=true`。

使用同机 Nginx 时，后端只绑定 `127.0.0.1`，Uvicorn 配置 `--proxy-headers --forwarded-allow-ips 127.0.0.1`，由可信代理还原实际连接来源；不要使用 `--forwarded-allow-ips '*'`。

Windows 启动脚本使用 `-ListenHost 0.0.0.0 -GatewayPort 80 -ApiPort 18000 -IamCallbackPort 8086`，不传 `-IamSyncPort`，与 `-MockIamPort` 互斥。生成配置后执行 `nginx -t`，8086 需允许用户浏览器访问。`deploy/cloud/compose.yaml` 仍是模拟 IAM 开发环境配置，真实部署使用这里的独立入口和可信代理配置，不能直接沿用其模拟 IAM 依赖。

## 原生 R10 首页票据兼容

原生 Nginx 仅在 GET `/`、`/admin`、`/admin/` 含有 `ticket` 参数时，303 转到 `/api/v1/auth/home-callback`。该入口验证 `ticket`、`state`、`scope` 各出现一次且格式有效，再 303 转到既有 `/api/v1/auth/callback`。使用浏览器跳转而非内部重写，是为了进入 `/api` 后能携带 Path 为 `/api` 的临时绑定 Cookie。

兼容入口不验票、不访问数据库、不消费登录状态，也不接受外部传入的 `service` 或返回地址。原回调继续从 Redis 读取最初保存的 service 和业务返回地址，验证浏览器绑定、验票并建立会话。普通页面、退出、角色管理和原回调规则保持不变。参数异常显示 `invalid_callback_parameters`，不清除已有有效会话。

此方案只兼容“返回路径误落首页但完整登录上下文和票据 service 未变”。缺少 state/scope、Cookie 不匹配、已使用的票据及 service 改写仍会失败；网关自行发起的无平台上下文票据不能直接登录。`home_callback/forwarded` 只证明转交，不证明 IAM 已验票。日志和诊断报告沿同一个状态摘要追踪后续步骤，不输出原始票据。

## 首次登录与管理员设置

1. 用户访问平台业务地址，没有会话时直接进入 IAM 登录；已有 IAM 会话时可由 IAM 免密返回。
2. 后端验票成功，以 `attributes.workNo` 对应 `users.iam_subject`，新建 `active/user` 的最小本地记录。姓名取可信验票响应，内部业务继续关联 `users.id` UUID。
3. 重复登录复用该 UUID，保留本地角色和已有姓名；不从验票报文、前端或 Mock 名单授予正式管理员。已有 `disabled/deleted/pending` 账号保持受限，不因登录自动激活。
4. 需要成为管理员的人员先登录一次，再由部署人员在后端目录、使用目标环境配置执行：

```bash
python -m scripts.set_user_role --work-no 10001 --role admin
python -m scripts.set_user_role --work-no 10001 --role user
```

`10001` 替换为真实 `workNo`，不是姓名、顶层 CAS `user` 或平台 UUID。命令使用当前 `.env` 的数据库，不存在的用户会报错，不会创建虚假身份。只能给正常启用的账号授权；可以撤销受限账号的管理员角色。重复设置相同角色不会重复记录变更。

角色修改与 `system_logs` 运维记录在一个事务提交，事件为 `auth_local_role_changed`，包含操作系统账号、机器、目标用户、前后角色。后续后端请求读取最新角色，无需重启或重新登录；前端刷新/聚焦后更新管理入口，撤权立即限制后端管理接口，正常用户端会话继续有效。

用户和管理端共享平台会话。普通用户第一次直接进入管理端也会建号、建立普通用户会话，但停留在无权限状态页，可返回用户端。正常访问不显示本地登录页；`/login` 仅保留认证错误、无权限、停用和主动退出后的状态与重试入口，避免循环重定向。

本期联调顺序：新用户自动跳转和回调 → 重复登录不重复建号 → 运维授权 → 两端同标签切换和共享会话 → 运维撤权 → 平台主动退出 IAM → 再次登录。验票失败、IAM 不可达、过期会话及不安全返回地址也需要验证。

## 后续启用身份同步

仅当用户与角色推送正式启用时，设置 `AUTH_IDENTITY_SYNC_ENABLED=true`、`AUTH_IAM_ADMIN_ROLE_CODE=platform_admin`、`AUTH_IAM_SYNC_ALLOWED_NETWORKS=["<推送服务器实际源IP>/32"]`，并额外配置 `--iam-sync-port 8085`（启动脚本为 `-IamSyncPort 8085`）。登记 `http://<平台主机>:8085/api/v1/auth/iam/identity-sync`，防火墙仅允许实际 IAM 推送来源。来源取可信 ASGI 连接地址，不自行信任任意转发头或 `appId`，空范围拒绝全部推送。

启用后 IAM 负责角色授权，本地角色命令拒绝执行，避免两套来源互相覆盖。切换前核对已有本地管理员并完成首次用户、角色对账；开关本身不会清空现有用户或管理员，也不能代替对账。

IAM-02 映射 `workNo`、`userNo`、`displayName`、`status`、`syncStatus`、`roleCode`、`syncTime`、`size`，只受理用户和本平台管理员角色。用户 `2000` 为正常、`3000` 为停用、`-1000` 为注销，`recycle`/`syncStatus=800` 为回收；角色 `authorized` 授权，`unauthorized` 或回收该角色撤权。角色变化不激活停用账号，撤权后正常账号仍可使用用户端。正式模式不使用 Mock 管理员名单，首次验票不能激活尚未同步的用户。

当前不维护组织树，不校验用户所属组织是否先同步；用户报文中的附加组织字段暂不落库。组织展示字段需要时再按实际来源补充。`org/dict` 独立批次、未知状态/角色不返回成功，不把未处理的数据误报为已同步。IAM 应按当前接入范围推送用户及本应用管理员角色。

身份同步后续联调再验证普通用户/管理员首次同步、撤权与停用、来源限制和同步回执兼容性，并核对 `syncTime` 单位及重试保留规则。用户和角色分别按原始 `syncTime` 比较，不用本机时间替代；`syncSequence` 不参与尚未确认的排序规则。

## 模拟账号

| 登录页名称 | IAM 标识 | 初始角色 |
| --- | --- | --- |
| 模拟用户A | `mock-user` | `user` |
| 模拟用户B | `mock-user-b` | `user` |
| 模拟用户C | `mock-user-c` | `user` |
| 模拟管理员A | `mock-admin` | `admin` |
| 模拟管理员B | `mock-admin-b` | `admin` |

A 账号沿用原有标识，历史记录归属保持不变。新增账号首次成功登录时按需建档，无需新增数据表。`AUTH_MOCK_ADMIN_SUBJECTS` 默认包含两个管理员，仅在插入新 Mock 用户时设置初始角色；已有用户包括运维撤权后的用户不再自动提权。名单在正式模式下完全无效。

## 会话规则

用户端和管理端共用 `ach_session` Cookie，路径为 `/api`，启用 HttpOnly 和 SameSite=Lax；HTTPS 环境需同时启用 Secure。管理端在后端逐次校验管理员准入，写请求校验 Origin 和 CSRF。

任一端本地退出、会话失效或本地账号停用后，两端共用的身份失效。`/logout` 默认先撤销本地会话，再返回 IAM `/logout` 地址，由前端同标签跳转；只有显式传 `global_logout=false` 才只退出本平台。退出状态页与跨标签退出通知不会立即自动重新认证。

正式模式不挂载 `/single-logout`，集团暂不提供通知，因此在其他系统退出 IAM 不会即时撤销平台已有会话。本期也不接收集团停用推送，平台现有会话按本地状态和固定有效期（默认 8 小时）校验，下一次 CAS 认证由 IAM 判定。不能宣称已完成集团账号变更的实时同步。

只接受共享 Cookie；旧 `ach_session_user/admin` 不再用于登录，登录或退出时清除。认证和身份同步的所有数据库操作使用单库连接。

| 表 | 职责 |
| --- | --- |
| `users` | UUID、唯一 IAM 标识、姓名、状态、角色、两类同步时间及本地创建/更新时间 |
| `auth_sessions` | 关联用户的共享会话、令牌/票据摘要、期限与撤销状态；也保存提前登出标记 |
| `operation_logs` | 成功操作的发起方、动作、对象、追踪编号和受控摘要；失败进入系统日志 |
| `iam_sync_receipts` | 身份同步启用后的批次回执，本期不新增同步记录 |

用户状态为 `pending/active/disabled/deleted`，角色为 `user/admin`。仅登录模式首次建号为 `active/user`，同步时间留空，平台不提供资料编辑入口。同步模式下角色推送不能激活账号，用户状态与角色各自比较 IAM `syncTime`。

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
| `/api/v1/auth/home-callback` | 原生首页票据转交；沿用原回调校验，不独立创建会话 |
| `/api/v1/auth/session` | 查询会话、CSRF 值及管理端准入 |
| `/api/v1/auth/logout` | 默认同时撤销平台会话并主动退出 IAM |
| `/api/v1/auth/single-logout` | 仅模拟 IAM 的单点登出通知 |
| `/api/v1/auth/iam/identity-sync` | 仅在同步启用且 IAM 模式时挂载 |
| `/api/v1/auth/mock/identity-sync` | 仅在同步启用且 Mock 模式时挂载 |

模拟身份同步使用 `X-Mock-IAM-Key` 校验来源，正式推送不使用该密钥。两者共用规范化业务报文摘要去重、来源时间乱序处理和事务逻辑。回执、用户更新、会话撤销和成功日志在同一事务提交。停用账号撤销会话，撤销管理员授权限制后续管理请求。用户与角色分别比较 `syncTime`；旧时间忽略，同时间不同内容返回 409，批次整体回滚。

模拟报文为 `appId=platform`、`resType=user/userlinkedrole`、`action`、`syncTime` 和最多 100 条 `data`。用户事件字段为 `subject/status/displayName`，角色事件为 `subject/roleCode/status`；能力码为 `platform_admin`。正式入口检查实际 AppID，并将 `AUTH_IAM_ADMIN_ROLE_CODE` 映射为此内部能力码。报文不含 `eventId`，重试必须复用原内容和 `syncTime`，不能生成新来源版本。模拟来源时间使用毫秒，正式单位仍待确认。

显式开启同步的本地环境可运行 `python -m scripts.mock_identity_sync --subject mock-user --status disabled`；恢复使用 `--status active`，失败重试使用 `--retry <事件文件>`。本期默认关闭时入口返回 404。事件文件在被忽略的 `.runtime/identity-sync/`，密钥只从本机配置读取。

隔离测试执行 `python -m pytest`。已启动模拟登录网关时，可运行 `node scripts/verify_auth_browser.mjs`；它会操作模拟会话。带 `VERIFY_IDENTITY_SYNC=1` 的验证还会临时修改模拟身份状态并恢复。

模拟来源密钥和正式会话密钥只保存在本机配置。应用错误不记录 CAS 票据和原始验票响应，网关日志使用不含查询参数的 `$uri`；直接启动 Uvicorn 时使用 `--no-access-log`，不得在联调中开启会输出完整验票 URL 的 httpx/httpcore 调试日志。
