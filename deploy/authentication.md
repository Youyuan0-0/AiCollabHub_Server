# 共享登录与身份同步

认证模块位于 `app/modules/common/auth/`，最小身份同步位于 `app/modules/common/identity_sync/`。当前支持开发环境中的模拟 IAM，正式集团 CAS、组织档案和完整角色映射仍需接入。

## 会话规则

用户端和管理端共用 `ach_session` Cookie，路径为 `/api`，启用 HttpOnly 和 SameSite=Lax；HTTPS 环境需同时启用 Secure。管理端在后端逐次校验管理员准入，写请求校验 Origin 和 CSRF。

任一端退出、会话失效或收到单点登出通知后，两端共用的身份失效。旧的分端 Cookie 仅用于兼容升级，不延长原会话有效期。认证和身份同步的所有数据库操作使用单库连接。

`auth_session` 保存会话，`auth_login_request` 保存短期登录上下文，`auth_event_log` 保存认证审计。身份同步另有账号状态、角色授权与事件回执三张表；它们不是完整人员档案。

## 本地联调

在根目录 `.env` 配置数据库后执行：

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

身份同步使用 `X-Mock-IAM-Key` 校验来源，并按事件编号去重、按源版本处理乱序。停用账号会撤销其会话，撤销管理员授权会限制后续管理端请求。报文与重试规则见 [最小身份同步](../README.md#最小身份同步)。

隔离测试执行 `python -m pytest`。已启动模拟登录网关时，可运行 `node scripts/verify_auth_browser.mjs`；它会操作模拟会话。带 `VERIFY_IDENTITY_SYNC=1` 的验证还会临时修改模拟身份状态并恢复。

模拟来源密钥只保存在本机配置；它不能替代正式 IAM 的签名与来源认证。日志避免记录 CAS 票据，直接启动 Uvicorn 时使用 `--no-access-log`。
