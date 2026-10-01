# 单服务器部署

当前架构为一个 Nginx 入口、两份前端产物、一套 FastAPI 服务，以及 PostgreSQL、Redis、MinIO 各一个实例。API 可在同机使用多个 worker，数据库读写统一访问同一实例。

## 云服务器 Docker 部署

`deploy/cloud/compose.yaml` 复用外部网络 `aicollabhub-cloud-dev_services`，不创建或删除基础服务。用户端 Nginx 作为公网入口，`/admin/` 转发到管理端容器，`/api/` 转发到 API，`/authn-api/v5/cas/platform/` 转发到独立模拟 IAM。三端各有 Dockerfile；API 镜像同时用于迁移和单进程 IAM。

开发云主机入口：

- 用户端：`http://1.92.86.74/`
- 管理端：`http://1.92.86.74/admin/`
- 新增安全组端口：TCP 80。浏览器不需要访问后端、IAM 或基础服务端口。
- 访问业务页面后自动跳转模拟认证页，选择普通用户或管理员。该环境保留开发模拟能力，未接入正式集团 IAM 或真实模型；正式配置见 [单点登录说明](authentication.md)。

### 构建和打包

云主机内存较小，先在开发机完成前端构建，再把产物放入 Nginx 镜像。当前工作树修改也会进入发布包，`manifest.json` 记录提交号与修改状态，不会自动提交 Git。

用户端 PowerShell：

```powershell
$env:VITE_ADMIN_URL='/admin/'
$env:VITE_API_MODE='http'
$env:VITE_API_SCOPE='all'
$env:VITE_API_BASE_URL='/api/v1'
pnpm install --frozen-lockfile
pnpm build
```

管理端 PowerShell：

```powershell
$env:VITE_BASE='/admin/'
$env:VITE_USER_URL='/'
$env:VITE_API_BASE_URL='/api/v1'
npm ci
npm run build
```

回到后端项目打包；默认从相邻 `AiCollabHub_User/dist`、`AiCollabHub_Admin/dist` 取产物，也可通过 `--user-dist`、`--admin-dist` 指定目录：

```powershell
.venv/Scripts/python.exe scripts/package_cloud_release.py --output .runtime/release.tar.gz
```

将发布包上传到服务器的 `/opt/aicollabhub-app/releases/<版本>/` 并解压，在该目录执行：

```bash
export DOCKER_BUILDKIT=0  # 仅适用于当前未安装 buildx 的云主机
docker build -t aicollabhub-api:<版本> backend
docker build -t aicollabhub-user:<版本> user
docker build -t aicollabhub-admin:<版本> admin
```

基础镜像以摘要固定。镜像站可用 `--build-arg PYTHON_IMAGE=<地址@摘要>` 或 `--build-arg NGINX_IMAGE=<地址@摘要>` 指定，保持与官方摘要一致。后端使用 Python 3.13，按 `requirements-dev.lock` 约束安装运行依赖。

### 云端配置与持久化

当前应用目录为 `/opt/aicollabhub-app`，其中 `compose.yaml` 来自本项目，`.env` 保存 `RELEASE_TAG`、`HTTP_PORT` 和 `SERVICES_NETWORK`。下列凭据文件权限均应为 `0600`，不进入镜像或版本库：

- `app.env`：参考根目录 `.env.example`；使用 `APP_ENV=test`、`AUTH_MODE=mock`、`DATASETS_PUBLICATION_MODE=local`。PostgreSQL 必须指向已确认的 43 表新库，不能沿用旧业务库地址；Redis 和 MinIO 使用本环境可达的服务入口。每个 API worker 配置 `DB_POOL_SIZE=5`、`DB_MAX_OVERFLOW=2`、`DB_POOL_TIMEOUT=5`，两个 worker 合计最多 14 条连接；总额度还须计入其他应用、迁移及运维连接。
- `iam.env`：仅包含认证相关变量和用于配置校验的 `DATABASE_URL=sqlite://`，模拟 IAM 不连接业务库。模拟 IAM 必须单进程运行。
- 两者共用随机 `AUTH_MOCK_SECRET`；公网 `AUTH_PUBLIC_ORIGIN`、`AUTH_MOCK_IAM_URL` 均为 `http://1.92.86.74`；`AUTH_INTERNAL_ORIGIN=http://api:8000`，`AUTH_MOCK_IAM_INTERNAL_URL=http://mock-iam:18002`。内网来源只用于票据校验和注销通知，浏览器回调始终保留公网来源。
- `migration.env`：仅包含 `POSTGRES_MIGRATION_*`，使用同一库的 DDL 账号；正常 API 容器不加载此文件。

开发联调数据库建议以 `max_connections=60`、`superuser_reserved_connections=3` 为连接预算起点，调整前核对主机及数据库容器的内存限额。普通账号共享剩余 57 条连接；例如三套各两个 worker 的后端合计最多 42 条，剩余 15 条供工具、迁移及其他连接使用。应用 `.env` 只控制本实例连接池，不会修改数据库服务端参数；服务端修改 `max_connections` 后必须重启 PostgreSQL，若参数来自 Docker 启动命令，则修改对应 Compose 配置并重建该数据库容器。

API 挂载 `./data:/data`，属主为 `10001:10001`，用于上传暂存和工具链接入配置。平台正式资料存 PostgreSQL，原件和预览存 MinIO，外部模拟运行态存 Redis；不再依赖旧应用快照或本地会话附件目录。新代码不会导入旧目录数据，旧环境文件须保留在其备份中。前端容器只读运行，API 使用两个 worker，模拟 IAM 独立单 worker。

反馈单附件上限为 1 GiB，用户端 Nginx 对 `/api/v1/feedback/attachments` 单独配置 1025 MiB 请求上限并关闭请求缓冲。发布前执行 `install -d -m 0700 -o 10001 -g 10001 data/tmp`；API 的 `TMPDIR=/data/tmp` 用于磁盘临时文件，上传结束会关闭并删除暂存文件，运行数据备份可排除此目录。

### 发布与检查

1. 暂停所有连接该库的旧版本后端和同步发送器，再备份数据库以及 `data/`，保留旧版本镜像和配置；首次部署备份在 `backups/pre-app-20260916-01.dump`。使用与服务端兼容的 `pg_dump` 并验证恢复。
2. 更新 `/opt/aicollabhub-app/.env` 的 `RELEASE_TAG` 并同步 Compose 配置，执行 `docker compose config --quiet`。
3. 确认连接目标为新结构数据库，执行一次 `docker compose run --rm migrate`，使用 `alembic -n resource_foundation upgrade head`。此入口不能把旧库原地转换为新库，发现旧表会停止；不允许用 stamp 跳过检查。确认迁移成功后再启动。
4. 执行 `docker compose up -d --wait --wait-timeout 120`。
5. 执行 `docker compose exec -T api python -m scripts.verify_services`，并验证两端登录、切换、注销、应用详情以及会话附件。

容器有健康检查、内存上限、日志轮转和 `unless-stopped` 重启策略。维护命令均在 `/opt/aicollabhub-app` 执行：

```bash
docker compose ps
docker compose logs --tail 100 api mock-iam
docker compose restart api
```

回退应用时，将 `RELEASE_TAG` 改回已保留的版本再执行 `docker compose up -d --wait`；先确认该版本兼容当前数据库结构。数据库恢复须按备份流程单独处理，不随应用回退自动降级或删除数据。模拟 IAM 重启会清空它的模拟单点登录状态，重新登录即可。

## 配置与数据

根目录 [.env.example](../.env.example) 提供配置字段，实际配置写入本机 `.env`。已有基础服务可直接填写连接信息；新建服务可使用 [compose.yaml](../compose.yaml)，它只编排数据库、缓存和对象存储。

- 数据库：填写 `POSTGRES_HOST/PORT/DB/USER/PASSWORD`，也支持优先级更高的 `DATABASE_URL`。
- 缓存与对象存储：按实际接入填写 `REDIS_*`、`MINIO_*`，并创建业务使用的 MinIO Bucket。
- 框架迁移：执行一次 `python -m alembic -n resource_foundation upgrade head`，不随 worker 启动重复执行。当前头为 `20260921_04_knowledge_external`，共 43 张业务表，见 [新库基线](../database/resource-foundation/README.md)。
- 配置 PostgreSQL 后五类资源统一装配新仓储，旧存储模式变量不再选择旧表。不要执行旧默认迁移链或场景应用建表 SQL。
- 业务文件在 MinIO，资料与引用在 PostgreSQL，备份时覆盖两者；Redis 承载共享登录上下文及外部模拟运行态。

## 前端构建

用户端在构建前设置 `VITE_ADMIN_URL=/admin/`，然后执行 `pnpm install --frozen-lockfile`、`pnpm build`。

管理端在构建前设置 `VITE_BASE=/admin/`、`VITE_USER_URL=/`，然后执行 `npm ci`、`npm run build`。

两端分别生成 `dist/`。Nginx 托管用户端 `/`、管理端 `/admin/`，将 `/api/` 转发到 API 监听端口，并为页面刷新提供 History 路由回退。

## 启动与网关

本地 Windows 联调使用以下命令；端口和 worker 数量按实际配置调整：

```powershell
.\scripts\start-local.ps1 -NginxRoot <Nginx目录> -ApiPort 18000 -Workers 2 -GatewayPort 18880
```

启用模拟登录时，先按 [认证说明](authentication.md) 配置，再追加 `-MockIamPort 18002`。脚本返回本次进程记录；停止时执行 `scripts/stop-local.ps1 -ProcessRecord <记录路径>`。

Linux 云服务器可由 systemd 等工具托管后端：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 18000 --workers 2 --no-access-log
```

[render_nginx.py](../scripts/render_nginx.py) 根据前端产物目录、监听地址和端口生成 [Nginx 模板](nginx/nginx.conf.template)。配置生成后执行 `nginx -t`，再由部署环境加载。域名、HTTPS 和服务托管参数在云服务器上配置。

当前业务仍包含模拟能力；`APP_ENV=production` 会关闭模拟业务路由，数据库看板与会话也尚未开放为正式业务。展示环境应按现有接入能力选择配置，不能仅切换环境名称就视为正式接入完成。

## 检查与维护

- `GET /api/v1/health/live`：进程存活检查。
- `python -m scripts.verify_services`：默认只读检查已配置的基础服务。
- `scripts/backup_database.py`、`scripts/backup_objects.py`：数据库和对象备份，恢复验证使用新建的隔离目标。
- `scripts/cleanup_auth.py`：按保留周期清理过期登录状态和审计记录。

会话迁移与附件保留细节见 [PostgreSQL 会话说明](conversations-postgresql.md)。
