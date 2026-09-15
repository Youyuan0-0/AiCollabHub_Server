# 框架联调与部署基础

更新时间：2026-09-13。当前按“无登录、无权限、无真实业务，只使用 HTTP Mock”的范围运行。用户端和管理端请求真实 FastAPI 接口，接口返回样例；基础服务的探测、复制和备份与页面数据分开验证。

## 当前入口和服务

用户端 `http://127.0.0.1:18880/`，独立管理端 `http://127.0.0.1:18880/admin/`。两端均直接访问，无需账号。Nginx 将 API 请求分配给 18000/18001 两个 FastAPI 进程。

基础服务在 `192.168.0.219`，开发电脑在同一局域网。仅在服务所在电脑连接时使用 `127.0.0.1`。

| 服务 | 地址 | 已有验证与当前使用 |
| --- | --- | --- |
| PostgreSQL A | 192.168.0.219:15433 / AiCollabHubTest | 16.14，已验证认证、读写和主备；Mock 页面不读写该库 |
| PostgreSQL B | 192.168.0.219:15434 / AiCollabHubTest | recovery 只读、streaming/sync、同步等待及写后可见性已验证；应用读路由仍关闭 |
| MinIO S3 | http://192.168.0.219:19002 | aicollabhub-files 桶，基础上传/下载/删除已验证；页面只发送文件元信息 |
| MinIO 控制台 | http://192.168.0.219:19003 | 已验证端口可达 |
| Redis | 待提供服务地址 | 客户端和部署模板已备好；此前目标主机 6379、16379 未响应 |

凭证只在被 Git 忽略的 `.env` 和本地运行目录中，不进入前端。旧真实业务联调报告保留为历史证据，不代表当前已实现真实反馈、会话或权限功能。

## 1. 启动 Mock 框架

后端 `APP_ENV=development` 或 `test` 即提供 Mock 接口，连接地址可留空。默认开发命令：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

用户端执行 `pnpm dev`，管理端执行 `npm run dev`，默认分别在 5173、5174，均将 `/api` 代理到 8000。用户端默认是 HTTP 模式，不需要单独开启后端联调开关。

多数 GET 返回固定列表，写操作返回模拟回执。场景应用的专用发布/审核接口新增内存暂存：同一实例中可跨请求读写，浏览器刷新保留，服务重启或热重载后重置。演示发布流程时，前后端请直连同一个 API 实例并只使用一个 worker；A/B 负载分流不能共享这些暂存记录。数据库/Redis/MinIO 不可用不影响 Mock 业务页面，只有健康检查如实显示未配置或不可用。

## 2. 配置并验证基础服务

新环境可使用已有服务凭证文件导入配置：

```powershell
.\.venv\Scripts\python.exe -m scripts.configure_services --source-env <可访问的服务.env路径> --host 192.168.0.219
.\.venv\Scripts\python.exe -m scripts.verify_services --output .runtime/services.json
```

导入脚本只更新被忽略的应用 `.env`，不会修改来源文件或打印密码。它初始化为关闭同步校验和读分流；现有已配置环境不要为刷新文档而重复导入。当前本地配置为 `APP_ENV=test`、`DB_REQUIRE_SYNC_APPLY=true`、`DB_READ_ENABLED=false`。

需要独立读写探测时显式执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_services --write-probes --create-bucket --output .runtime/service-probes.json
```

探测使用独立随机名测试表和对象并清理；只有 `--create-bucket` 会创建缺失的桶。综合命令在 Redis 未配置时可能退出 1，应逐项读取报告，不能据此把 PostgreSQL 或 MinIO 判为失败。

基础迁移 `20260913_01` 已改为空操作，不再创建账号、反馈或文件表。当前代码不会删除远端历史测试数据，Mock 路由也不读取它们。

## 3. Redis 服务

在有 Docker Compose 的服务电脑运行独立 Redis 配置。服务环境文件需设置 `REDIS_PASSWORD`、`REDIS_PORT=16379`，局域网绑定使用 `REDIS_BIND_ADDRESS=192.168.0.219` 并限定网络来源。

```powershell
docker compose --env-file <服务.env路径> -f deploy/test/compose.redis.yaml up -d --wait
```

两个 API 实例使用同一 `REDIS_URL`、环境和键前缀。已有客户端支持 TTL 和失败按未命中处理，Mock 页面不使用 Redis。当前尚未部署 Redis 服务；真实缓存读写、过期和跨实例访问仍待联调。业务缓存失效、锁和任务语义随正式模块设计，不由这个通用客户端代替。

## 4. 主备及写入等待重放

主库已配置 `synchronous_standby_names='FIRST 1 ("AiCollabHubTest-B")'`，平台数据库使用 `synchronous_commit=remote_apply`。实际备库身份来自 `pg_stat_replication`，不根据端口猜测。配置模板在 `postgresql/`，当前环境无需重新初始化主备。

此前在测试集群已验证暂停/恢复重放、写入等待、恢复后新查询可见；也验证了提交期限到期取消、结果不确定及连接池后续可用。记录在 `.runtime/integration-20260913/`。复验会短暂暂停备库重放，只在隔离测试或维护窗口执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_sync_apply --pause-standby-for-test --output .runtime/replay-check.json
```

`DB_REQUIRE_SYNC_APPLY=true` 检查数据库有效同步策略，`DB_COMMIT_TIMEOUT_MS` 默认 10000。PostgreSQL 16 提交阶段不受 statement timeout 保护，框架通过独立期限取消等待。超时或断连可能发生在本地主库提交后，返回 `commit_outcome_unknown`，不能推断已回滚或直接重复执行；后续真实业务需要自己的幂等与结果核对。

当前 `DB_READ_ENABLED=false`，基础设施探测的常规读写遵循主库连接配置；主备探测另行直连备库查询。后续开启显式只读路由时使用 `DB_READ_WEIGHT` 调整比例，所有写入、行锁及先读后改事务仍走主库。必须确认只读目标参与所要求的同步确认，后续查询使用新快照。

当前两个数据库实例位于同一物理机，尚未验证两台物理服务器容灾、自动接管或目标容量。主备复制也不替代独立历史备份。

## 5. 构建前端与 Nginx 分流

在用户端工程：

```powershell
$env:VITE_ADMIN_URL='/admin/'
pnpm build
```

在管理端工程：

```powershell
$env:VITE_BASE='/admin/'
$env:VITE_USER_URL='/'
npm run build
```

在后端工程启动本地双实例：

```powershell
.\scripts\start-local.ps1 -NginxRoot <已解压的Nginx目录> -ApiPortA 18000 -ApiPortB 18001 -GatewayPort 18880
```

脚本拒绝占用已有端口，并输出本次进程记录。用户端 `/`、管理端 `/admin/`、API `/api/` 共用入口。`X-App-Instance` 标识后端实例；`X-Request-ID` 用于定位请求。使用本次生成的记录停止服务：

```powershell
.\scripts\stop-local.ps1 -ProcessRecord <本次启动生成的processes.json>
```

可加 `-Role A` 仅停止 A。本地浏览器已验证 A/B 均处理 Mock 请求；旧基础设施联调另验证过 A 退出后 B 继续响应。Nginx 使用静态权重和被动失败摘除，不对已发送的非幂等请求启用自动重试。

两台服务器部署时另行设置 upstream、监听地址、TLS、可信代理与服务管理。当前本地两个进程的验证不包含物理节点故障或 5000 并发验收。

## 6. 独立备份工具

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_database --directory <独立数据库备份目录> --pg-bin <PostgreSQL客户端bin目录> --restore-check
.\.venv\Scripts\python.exe -m scripts.backup_objects --directory <独立对象备份目录> --prefix framework-probes/ --restore-bucket <全新的验证桶名>
```

数据库脚本动态枚举非系统表，计数与 pg_dump 使用同一个只读快照，生成自定义格式备份及 SHA-256 清单。`--restore-check` 创建新隔离数据库并核对表计数，不覆盖源库；恢复库保留供核对。它不包含角色、WAL、增量备份或时间点恢复。

对象脚本按显式前缀备份，不再查询业务文件表。默认只处理 `framework-probes/` 下测试对象，本地使用随机文件名；恢复必须使用全新桶，逐文件核对 SHA-256。没有对象时不能将空恢复记为验证成功。它不包含全部历史版本，也不保证与数据库处于相同快照。

旧版本脚本曾完成真实测试数据的数据库/MinIO 恢复；本轮通用脚本与业务模型解耦后的远端恢复尚未复验。正式独立存储、保留周期及定时环境未提供，因此尚未安装定时备份。

## 7. 本轮验收

`node scripts/verify_framework_browser.mjs` 检查桌面 1440×960、手机 390×844 的 10 个页面、直接访问、HTTP 数据、模拟反馈提交/处理、A/B 请求分配及页面异常。结果和截图在 `.runtime/framework-mock-20260913/`，本轮全部通过。

后端 Mock 契约测试对数据库、Redis 和 MinIO 调用设置失败断言，验证页面路由不会接触真实服务。场景应用可在同一实例的用户端与管理端接口间暂存流转，尚未实现数据库持久化。

尚待完成：Redis 部署与实测、通用备份工具的远端复验、正式读分流启用、两台物理服务器部署及容量/故障验收。登录、权限、真实平台业务和第三方调用按用户要求暂不开展。
