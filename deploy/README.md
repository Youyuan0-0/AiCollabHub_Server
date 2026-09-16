# 单服务器部署

当前架构为一个 Nginx 入口、两份前端产物、一套 FastAPI 服务，以及 PostgreSQL、Redis、MinIO 各一个实例。API 可在同机使用多个 worker，数据库读写统一访问同一实例。

## 配置与数据

根目录 [.env.example](../.env.example) 提供配置字段，实际配置写入本机 `.env`。已有基础服务可直接填写连接信息；新建服务可使用 [compose.yaml](../compose.yaml)，它只编排数据库、缓存和对象存储。

- 数据库：填写 `POSTGRES_HOST/PORT/DB/USER/PASSWORD`，也支持优先级更高的 `DATABASE_URL`。
- 缓存与对象存储：按实际接入填写 `REDIS_*`、`MINIO_*`，并创建业务使用的 MinIO Bucket。
- 框架迁移：发布时执行一次 `python -m alembic upgrade head`，不随 worker 启动重复执行。当前唯一迁移头为 `20260916_01`。
- 场景应用：新库按 [数据库说明](../database/scenario-applications/README.md) 初始化 8 张业务表，再启用 `APPLICATIONS_STORAGE=database`。内存模式使用单 worker。
- `APPLICATIONS_DEMO_DIRECTORY` 中的供应方模拟快照和 `CONVERSATION_ATTACHMENT_DIR` 中的附件属于运行数据，须保留在固定路径。

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
