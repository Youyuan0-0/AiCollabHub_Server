# 单点登录联调

本轮实现平台侧登录、退出与单点登出，供应方使用独立模拟 IAM。身份同步、完整角色权限、个人资料页和集团真实 CAS 联调尚未实现。数据集、模型、场景应用、知识库、AI 工具、反馈及管理业务仍为 HTTP Mock。

## 数据与边界

认证模块使用三个表：

| 表 | 内容 |
| --- | --- |
| `auth_session` | 应用范围、供应方用户标识、展示姓名、会话/票据摘要、有效期与撤销时间 |
| `auth_login_request` | 短期登录上下文、浏览器绑定、原页面和消费状态 |
| `auth_event_log` | 登录、退出、单点登出的用户标识、事件、结果、时间与请求编号 |

没有本地用户档案、密码、手机号、身份证等字段。两套前端不保存会话令牌，令牌仅通过 HttpOnly、SameSite=Lax Cookie 传递；HTTPS 环境必须启用 Secure。用户端、管理端使用不同会话 Cookie；单点登出按原 CAS ST 撤销关联会话。认证读取和写入均访问主库，写入复用现有 `remote_apply` 检查与提交期限处理。

临时登录请求通过条件更新原子消费。登出通知先到时也保存短期撤销标记，避免迟到的验票结果重新创建会话。收到不确定提交错误时，不确认登录或退出成功，不自动重放票据；登录需重新发起，退出可以重试并重新检查会话。

模拟 IAM 只监听本机，在单独的进程保存测试账号、一次性票据和 IAM 会话。模拟器重启会丢失它自己的状态；已有平台会话仍按数据库有效期处理，完整演示应重新登录。它不是生产高可用 IAM，也不是完整协议认证产品。

## 本地启动

首页分支合并后，统一迁移链为 `20260913_01 → 20260914_01 → 20260914_02 → 20260914_03`。原认证分支和原首页分支曾共用 `20260914_01`，最后一步按现有表补齐缺失的认证或看板表，保留已有数据。已有库直接执行 `alembic upgrade head`，不需要删库或重新 stamp。首页看板、问答和场景应用共用登录及 CSRF 检查，健康检查保持独立。

先在忽略的 `.env` 中配置 PostgreSQL 主库；认证可与现有主备测试环境共用，不修改复制设置。以下命令在后端仓库执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-dev.lock
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.configure_mock_auth --gateway-port 18880 --iam-port 18002
```

配置生成器只写 `.runtime/mock-auth.env`，保留原有服务凭证。该文件包含两个 API 和模拟 IAM 共用的随机联调密钥，禁止提交。子进程同时读取该文件和项目 `.env` 中的基础服务配置。

构建两个前端。在用户端仓库设置 `VITE_ADMIN_URL=/admin/` 后运行 `npm run build`；在管理端仓库设置 `VITE_BASE=/admin/`、`VITE_USER_URL=/feedback` 后运行 `npm run build`。随后启动：

```powershell
.\scripts\start-local.ps1 `
  -NginxRoot .runtime/integration-20260913/nginx-1.30.4 `
  -ApplicationEnvFile .runtime/mock-auth.env `
  -ApiPortA 18000 -ApiPortB 18001 -GatewayPort 18880 -MockIamPort 18002
```

使用启动输出中的进程记录，通过 `scripts/stop-local.ps1 -ProcessRecord <记录路径>` 停止本次服务。默认端口被占用时，脚本拒绝启动，不会自动终止其他进程；更换端口后须重新生成一致的认证配置。

- 用户端：`http://127.0.0.1:18880/`
- 管理端：`http://127.0.0.1:18880/admin/`
- 模拟 IAM：端口 `18002`，从平台登录入口跳转进入。

点击“统一身份登录”后可选择模拟普通用户或模拟管理员，不需要密码。管理端只接受 `AUTH_MOCK_ADMIN_SUBJECTS` 指定的模拟身份。普通用户会收到拒绝提示；“切换账号”重新进入模拟账号选择。正式集团管理员角色映射尚未实现。

当前会话返回 `user.canAccessAdmin`，用户端仅在该值为 `true` 时显示“管理工作台”链接。入口显隐与管理端后端准入共用同一判断；普通用户、未登录或认证关闭时不显示入口。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/auth/config` | 是否启用认证，前端启动时读取 |
| `GET /api/v1/auth/login?scope=user&return_to=/applications` | 发起登录，支持 `scope=admin` 和 `renew=true` |
| `GET /api/v1/auth/callback` | 校验浏览器上下文、原子消费登录请求、后端验票并创建会话 |
| `GET /api/v1/auth/session?scope=user` | 当前身份、有效期和 CSRF 校验值；未登录返回 401 |
| `POST /api/v1/auth/logout?scope=user` | 校验 Origin/CSRF，撤销本地会话，返回 IAM 退出地址；`global_logout=false` 可只退出当前应用 |
| `POST /api/v1/auth/single-logout` | 接收表单 `logoutRequest` 内的 SAML XML；验证模拟通知来源并撤销 ST 对应会话 |

模拟 IAM 沿用文档 `/authn-api/v5/cas/{appId}/login`、`/p3/serviceValidate`、`/logout` 形式。`appId=platform` 仅是模拟器登记值。验票与通知使用 `X-Mock-IAM-Key`，这不是集团签名协议。实际 IAM 地址、应用登记、签名 SDK、回调网络和通知来源认证齐备后，才能实现真实适配与验收。

登录页面和回调地址由 `AUTH_PUBLIC_ORIGIN` 确定，不信任请求 Host 拼接地址。返回路径限定在对应前端范围，禁止站外跳转。API 进程关闭包含查询串的访问日志，由 Nginx 记录不含查询串的 `$uri`。直接运行 uvicorn 时也须使用 `--no-access-log`，避免记录 CAS 票据。

## 验证与维护

```powershell
.\.venv\Scripts\python.exe -m pytest
node scripts/verify_auth_browser.mjs
.\.venv\Scripts\python.exe -m scripts.cleanup_auth
```

Python 测试使用独立 SQLite 文件模拟两个 API 共享持久存储，并验证迁移往返及旧表保留。浏览器脚本面向本地真实 PostgreSQL 网关，使用 18000/18001 两个实例验证会话与退出一致性，截图和结果写入忽略的 `.runtime/auth-verification`。没有完成两台物理服务器或容量验收。

清理脚本删除超过保留窗口的登录请求、会话和审计日志。认证日志默认保留 90 天，这只是开发默认值，正式保留期限须按甲方审计要求确定。脚本未配置自动调度；后续部署时由运行环境统一安排。清理仅操作新增的三个表。
