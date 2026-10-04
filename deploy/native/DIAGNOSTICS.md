# R10 认证诊断使用说明

适用版本：`20261004-native-r10`。本说明单独交付，不放入升级 ZIP。

诊断入口是 `sudo /opt/aicollabhub/diagnose.sh`。使用随包 Python，无须联网安装依赖。脚本不会修改配置、重启服务、修改角色或清空数据库。检查登录入口会生成短期 Redis 登录状态，但不会提交 ticket 或完成登录。

诊断工具单独安装到 `/opt/aicollabhub/diagnostics`，不依赖后端进程或应用虚拟环境。若升级中途失败、尚未安装该入口，也可在新包解压目录运行 `sudo bash ./diagnose.sh collect`，前提是旧版本的独立 Python 运行时仍可用。

## 1. 升级后先检查

```bash
sudo /opt/aicollabhub/diagnose.sh check
```

检查当前版本、服务、监听端口、时间、生效配置、本机 Nginx、网站静态资源、后端依赖、认证接口，以及服务器到平台外部域名和 IAM 的网络。每一项结束后立即输出状态，失败也继续后面的检查。

如果现场暂时不允许访问外部域名，或只想确认平台自身：

```bash
sudo /opt/aicollabhub/diagnose.sh check --local-only
```

每次生成独立目录，例：

```text
/var/log/aicollabhub-diagnostics/20261003-160000-a1b2c3d4
```

以实际输出为准。目录包含中文 `report.txt`、可在浏览器离线打开的 `report.html`、完整结构化 `report.json`、`diagnostics.tar.gz` 及其 SHA256 文件。完整说明不在 ZIP 内，诊断程序的 `--help` 保留必要参数帮助。

## 2. 跟踪一次真实登录

先在服务器执行：

```bash
sudo /opt/aicollabhub/diagnose.sh start
```

保存最后输出的报告目录和 `finish` 命令。开始时间在基线检查完成后记录，避免把脚本自身探测当作本次人工登录。

然后在用户电脑的浏览器打开：

```text
https://fdj-smartweb.aecc.cn/diagnostics/auth.html
```

这是独立诊断页面，不依赖 Vue、模块脚本、AbortSignal.timeout 或外部 CDN。Chrome 58 即使无法运行业务页面，仍可运行这里的基础诊断。实际入口以脚本根据生效配置输出的地址为准。

页面会记录：

- 浏览器 User-Agent、模块脚本能力、AbortController、AbortSignal.timeout、fetch、sessionStorage。
- XHR 和 fetch 分别读取认证配置的结果，包含耗时、响应类型及后端请求编号。
- 当前会话响应；401 表示尚未登录，403 表示该入口权限不足，均不等同于 IAM 不可用。
- 请求被取消、网络错误、超时、返回 HTML 或 JSON 无法解析等情况。

操作顺序：

1. 选择用户端或管理端。
2. 保持“重新认证”选中，点击“发起登录”。这样后端不会因为已有平台会话而跳过 IAM。是否要求重新输入账号，由 IAM 对 `renew` 的实现决定。
3. 在 IAM 完成正常认证。预期最终回到诊断页，自动检查平台会话。
4. 点击“打开业务页面”，核对首页或管理端是否能正常显示；自检页成功不能替代业务页面验收。
5. 回到同一标签页的诊断页，点击“导出诊断”，得到 `aih-browser-diagnostics.json`。记录只保存在该标签页的 sessionStorage，不保存 ticket、Cookie 值或 CSRF 值。
6. 在服务器执行 `start` 实际输出的 `finish` 命令。例如：

```bash
sudo /opt/aicollabhub/diagnose.sh finish /var/log/aicollabhub-diagnostics/20261003-160000-a1b2c3d4
```

如果浏览器 JSON 已放到服务器上，可一起归并：

```bash
sudo /opt/aicollabhub/diagnose.sh finish /var/log/aicollabhub-diagnostics/20261003-160000-a1b2c3d4 --browser-report /root/aih-browser-diagnostics.json
```

无法将浏览器文件放到服务器时，分别提供服务器压缩包和浏览器 JSON 即可。不要导出未经脱敏的原始 HAR、Cookie 或 ticket。

如 IAM 返回带 ticket 的 `/`、`/admin` 或 `/admin/`，R10 会在服务器侧转交原回调；正常情况下仍回到登录前保存的诊断页。没有成功时，记下最终地址的协议、主机、路径和参数名称，省略参数值，再在同一标签页重新打开诊断页导出记录。后端访问日志会记录首页是否携带 ticket，不记录票据原文。

如果诊断页本身被统一网关拦截到 IAM，服务器报告仍可生成；此时需要对方结合该时间段的网关日志核对转发。

## 3. 没提前 start，也能收集

收集最近 15 分钟平台日志，不重新运行网络检查：

```bash
sudo /opt/aicollabhub/diagnose.sh collect --minutes 15
```

可指定 1 到 1440 分钟。不要把无关用户一整天的日志都当成同一次登录，优先使用较短时间段或 `start` / `finish`。

每次最多读取 2000 条 journal 和 2000 条访问记录，访问日志读取当前文件及上一份未压缩轮转文件的尾部。报告会标记可能截断；日志轮转、服务重启或保留期限可能导致记录不足。要保留一份失败现场，再进行重试；再次 finish 会更新同一目录的报告。

## 4. 怎么看报告

| 状态 | 含义 |
| --- | --- |
| PASS | 当前检查有符合预期的证据 |
| FAIL | 有明确错误或结果不符合接口约定 |
| WARN | 存在异常或需要人工核对，不能仅凭此判定根因 |
| NOT_OBSERVED | 当前采集范围没有相应证据；不等同于服务失败 |
| NOT_EXECUTED | 上游已有明确失败，后续步骤没有执行证据 |

登录流程编号来自随机登录状态的截短摘要，仅用于日志关联，不具备登录能力。验票日志通过请求编号关联到回调；后续会话检查通过内部会话 UUID 关联到创建会话事件，不使用 Cookie 原值。

普通回调流程依次列出：发起新登录、收到回调、浏览器绑定、IAM 验票、平台用户、创建会话、携带会话访问。首页兼容流程会在“发起新登录”后增加“首页票据转交”，共八步；转交成功仅表示参数已交给原回调。报告还保留独立的会话复用、拒绝访问、参数异常和退出事件。

| 结果 | 优先核查 |
| --- | --- |
| 发起登录后没有回调 | 浏览器最终地址、发出 service 与 IAM 返回地址、网关转发；不能直接说验票失败 |
| 首页有 ticket，但没有首页票据转交记录 | 确认已运行 R10、请求是否到达平台 Nginx，以及原始请求是否为 `/`、`/admin`、`/admin/` 的 GET |
| invalid_callback_parameters | 首页票据参数缺失、重复或格式错误；需要保留唯一的 ticket、state、scope，再重新发起登录 |
| 首页票据转交成功，但后续失败 | 按后续浏览器绑定、IAM 验票和会话步骤定位；转交本身不创建会话，不会变更原 service |
| 回调 422，参数不存在 | IAM/网关是否保留 state、scope、ticket；结合访问日志参数存在标记 |
| login_request_expired | 绑定 Cookie 缺失、域名/协议变化、状态过期、重复回调或 Redis |
| cas_validate timeout/network_error | 平台后端到 IAM 的 DNS、端口、路由、证书及响应时限 |
| cas_validate http_status | 对方接口或网关的实际 HTTP 状态 |
| invalid_response/missing_work_no | 是否返回 HTML、报文结构、用户编号字段；脚本不导出供应方原始身份报文 |
| 已创建会话，后续没有会话访问 | Cookie 的设置和携带、前端是否发请求、日志时间是否覆盖完整 |
| session 200，但业务页报错 | 浏览器自检、JS 错误、响应正文类型、缓存与静态资源，不要重复归因于 IAM |
| 普通账号访问管理端 403 | 权限不足是正常结果，按正式运维流程授权后再测 |

HTTP 200 仅是响应状态，报告会同时检查 JSON/HTML 类型与基本结构。网络连接成功不代表 IAM 验票成功；服务器能访问入口不代表用户电脑能访问入口。

首页兼容不会为没有本平台登录上下文的票据补造 state，也不会跳过 Cookie 校验。若 IAM/网关改变了签发票据时的 service，必须修正对接配置；反复刷新旧票据不能修复。

## 5. 退出验证

在诊断页点击“退出登录验证”并确认。它会读取当前会话，使用真实 CSRF 校验退出，检查本地会话变为 401，然后跳转 IAM 退出地址。退出只在用户主动点击确认后执行。

回到诊断页导出结果。`local_session_revoked` 证明平台会话撤销；`iam_global_logout_not_verified` 表示脚本不能据此宣称 IAM 全局会话已清除。集团当前未提供正式单点登出通知能力，不能把未收到该通知报成平台故障。

## 6. 配置与证书问题

报告读取 `app.env` 为准，并与 `app-env.json`、`site.json` 比较。这里是磁盘配置证据，不等同于进程内所有配置已热更新；对照 API 启动时间确认修改后是否重启。

HTTPS 始终校验证书，不提供忽略证书验证的“通过”选项。如果现场使用专用根证书，可显式提供 CA 文件：

```bash
sudo /opt/aicollabhub/diagnose.sh check --ca-file /root/company-ca.pem
```

CA 文件仅供此次诊断，不修改系统信任库或应用配置。

脚本不自动修复现场配置。需要调整时，先保留失败报告及现有配置备份，再按报告指出的位置修改，验证 Nginx 语法、显式重启或重载对应服务，最后重新 check。不要反复刷新携带旧 ticket 的地址。

## 7. 交回哪些文件

- 服务器报告目录中的 `diagnostics.tar.gz` 和同名 `.sha256`。
- 浏览器导出的 `aih-browser-diagnostics.json`，若已导入服务器报告，可不重复提供。
- 说明本次测试是普通用户还是管理员、期望访问什么页面，以及实际屏幕表现。

诊断目录仅 root 可读，不作为网站静态目录发布。压缩包只含生成的三份报告，不包含 env、数据库、私钥、原始 HAR 或原始浏览器响应。日志会去除常见认证信息和已知部署凭据；仍应按内部运维资料管理。

退出码：0 表示基础检查未出现 FAIL，不代表完整 SSO 已验收；1 表示基础检查失败，或 finish/collect 采集到认证流程失败；2 表示命令参数、文件格式或权限等导致工具无法完成。必须结合流程表和浏览器业务页判断。
