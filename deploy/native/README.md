# 麒麟 V10 SP3 原生离线安装与更新

当前交付版本为 `20261004-native-r10`，包含首页票据兼容和离线认证诊断工具，不含压测工具；操作说明和验收记录独立交付，不放入 ZIP。复用已验证的 Python 3.13.15、PostgreSQL 17.11、Redis 7.4.11、MinIO 和 Nginx 1.30.5，包含三端代码及全部 Linux Python 依赖。服务器不需要 Docker、Node.js、编译器或外网下载。

适用麒麟高级服务器 V10 SP3 2403、x86_64、glibc >= 2.28、systemd。源码提交、工作区状态、运行时和升级来源记录在 `manifest.json`。仍为 `rehearsal/test` 联调环境，模型执行仍为 simulation；真实 IAM 不代表全部业务供应方已接入。

## 本次修复

- R10 在原生 Nginx 的 `/`、`/admin`、`/admin/` 收到带 `ticket` 的 GET 请求时，经 `/api/v1/auth/home-callback` 转交原回调。正常首页和其他业务路径保持原有处理；无需先加载前端 JavaScript。
- 兼容入口仅转交唯一且格式有效的 `ticket`、`state`、`scope`，保留原有浏览器绑定、原始 service 验票及权限检查；参数异常显示明确错误，诊断报告增加“首页票据转交”步骤。以下 R8/R9 修复继续包含在包内。
- 两端不再依赖 `AbortSignal.timeout/any/throwIfAborted/reason`，保留超时、取消和响应读取超时。构建语法以 Chrome/Edge 90 为基线，同时修复生产业务中的 `randomUUID`、数组 `at`、`structuredClone` 直接调用。
- 平台会话检查失败与 IAM 验票失败分别提示，不再把浏览器异常统一显示为 IAM 服务不可用。
- 支持网关终止 HTTPS、平台 HTTP 80/8086 同时提供完整网站，升级不会把这种拓扑的 8086 恢复成仅回调入口。
- 升级以 systemd 实际读取的 `app.env` 中 AUTH 字段为准，优先于历史 `app-env.json`，保留已有会话密钥；显式 `--auth-config` 优先级最高。不执行配置中的 shell 内容。
- 增加脱敏认证阶段日志和 Nginx 上游请求编号，不记录 ticket、Cookie、service 查询串或个人身份字段。

本次无新增数据库迁移。R3 至 R10 的目标结构均为 `20260924_01_field_alignment`，保留库名、业务数据和凭据。R2 沿用包内已有迁移。唯一迁移入口是 `alembic -n resource_foundation upgrade head`；禁止删表、stamp 或运行旧迁移链。

## 现场拓扑

浏览器访问 `https://fdj-smartweb.aecc.cn`，统一身份认证网关终止 HTTPS，再以 HTTP 转发到 `20.1.4.240:8086`。平台 Nginx 的 80 和 8086 都提供用户端、管理端、静态资源和同源 API；后端仅监听 `127.0.0.1:18000`。

对外登录回调为 `https://fdj-smartweb.aecc.cn/api/v1/auth/callback`。8086 是内部转发端口，不写进浏览器回调 URL。代码生成的 `service` 还包含 `state`、`scope` 参数，登录和验票必须使用原始同一个值。

IAM 地址基于 `http://hfidaas-sso.aecc.cn/authn-api/v5/cas/796872537136820224`：`/login`、`/p3/serviceValidate`、`/logout`。本期身份同步关闭，8085 不监听。管理员由本地运维命令设置。

## 现场 R6/R7/R8/R9 升级到 R10

R6（`20261001-native-r6`）和 R7（`20261001-native-r7-no-loadtest`）已分别使用原始交付包建立旧版环境，完成直接升级到 R10 的隔离麒麟验收；无需先安装中间版本。测试覆盖实际 `app.env` 与旧 JSON 不一致、手工开放 8086、HTTPS 网关、数据和已有会话保留。记录见独立交付的 `R6、R7直接升级验收.md`；现场仍先执行下述预检。

将 ZIP 导入服务器，例如 `~/packages`，不要覆盖旧包目录。校验清单 `SHA256SUMS` 已包含在 ZIP 内，覆盖全部 343 个包内文件，无需另外传送 `.sha256` 文件；解压后先校验，全部通过再升级：

```bash
cd ~/packages
unzip AiCollabHub-KylinV10SP3-x86_64-20261004-native-r10.zip -d ~/aih-packages
cd ~/aih-packages/AiCollabHub-KylinV10SP3-x86_64-20261004-native-r10
sha256sum --check --strict SHA256SUMS
```

该命令应显示各文件 `OK` 并成功退出；出现文件缺失、校验失败或解压错误时，先重新传送完整升级包，不继续升级。

没有 unzip 时可用 `python3 -m zipfile -e 文件.zip 目录`，或在 Windows 解压后完整导入。针对上述现场网关拓扑，进入新包目录后执行：

```bash
sudo bash ./update.sh --network-config ./gateway.example.json --auth-config ./auth.gateway.example.json --check
sudo bash ./update.sh --network-config ./gateway.example.json --auth-config ./auth.gateway.example.json
sudo /opt/aicollabhub/manage.sh check
```

第一条仅预检，不停止服务；通过后执行第二条。安装器自动暂停服务、备份数据库/文件/缓存/凭据/配置/旧版本指针，更新后检查服务。备份位于数据目录同级的 `aicollabhub-backups`。

网络模板只修改外部地址、网站监听端口和 TLS 位置，不能改库名、数据目录或内部服务端口。认证模板只明确启用 IAM、关闭同步、对齐 HTTPS 地址与 Secure Cookie；已保存的 IAM 地址、AppID、密钥继续复用。

无需网关调整的环境可执行 `sudo bash ./update.sh --check` 和 `sudo bash ./update.sh`。旧模拟模式默认切换 IAM；只有独立演练环境通过 `--auth-config` 显式指定 mock。不要把演练地址、证书、账号配置导入现场。

已完成的 R10 可重复升级，后续不带覆盖参数也会使用保存后的网关配置。支持的旧版以 manifest 的 `upgrade_from` 为准，不接受未知来源、降级或跨运行时更新。

## 验收和排查

`manage.sh check` 检查服务、43 张业务表、迁移版本、权限、Redis/MinIO 读写、网站静态资源、会话接口匿名 401 和登录入口 303。网关模式直连本机 80/8086 并携带正确 Host，不通过外部 IAM 网关验收，也不消耗 ticket。检查通过仅表示平台本机入口就绪。

新增详细诊断入口 `sudo /opt/aicollabhub/diagnose.sh check`。真实登录取证使用 `start` / `finish` 和 `/diagnostics/auth.html` 浏览器自检页。具体步骤见独立的 `认证诊断使用说明.md`，该文档不在 ZIP 内。

真实 IAM 仍需内网浏览器验收，网络面板开启“保留日志”：

1. 新会话访问统一域名，应请求 `/api/v1/auth/config`、`/api/v1/auth/session`，未登录时由 `/api/v1/auth/login` 跳转 IAM。
2. 登录后返回完整 `/api/v1/auth/callback?state=...&scope=...&ticket=...`；平台后端用原 service 验票，读取有效 `attributes.workNo` 后建立用户和会话。
3. 回调返回 303 和 `ach_session` Cookie，转到不带 ticket 的业务地址，随后会话接口返回 200，页面显示用户。
4. 已有会话时登录入口可直接 303 回业务页，不再次验票。普通用户管理端准入被拒绝，管理员正常进入。退出后本地会话失效；IAM 全局退出仍需现场确认。

首页 `/?ticket=...` 不能单独证明认证成功。R10 兼容的是返回路径变成首页，但仍保留本平台登录请求的 `ticket`、`state`、`scope`，且票据仍绑定原 service 的情况。转交流程如下：

```text
/?ticket=...&state=...&scope=user
  -> 303 /api/v1/auth/home-callback?...（浏览器进入 /api 后携带绑定 Cookie）
  -> 303 /api/v1/auth/callback?...（沿用原验票流程）
  -> 303 原业务地址（移除登录参数）
```

不会把首页 URL 当成新的 service，也不接受 URL 传入的 service 或返回地址。若网关在平台发起登录之前自行签发首页票据、丢失 state/scope，或实际签发的 service 已改变，本兼容不能建立会话；需保留原登录上下文、修正 IAM/网关配置并重新登录。Secure Cookie 仍要求浏览器使用统一 HTTPS 域名。单纯转交成功不等于验票或会话成功。

```bash
sudo journalctl -u aih-api --since "10 minutes ago" --no-pager
sudo tail -n 80 /data/aicollabhub/web/access.log
```

后端关键字：`login/redirect_to_iam`、`login/session_reused`、`home_callback/forwarded`、`home_callback/rejected`、`callback/received`、`cas_validate/started`、`cas_validate/success`、`callback/session_created`。验票失败区分 `timeout`、`network_error`、`http_status`、`invalid_response`、`missing_work_no` 等，不打印原始报文。

Nginx 的 `api_request_id` 对应响应 `X-Request-ID` 和后端 `request_id`，`upstream_status`/`upstream_time` 区分代理与后端。首页 HTML 的 200 不代表验票成功。不要转发原始 ticket、Cookie 或完整配置文件。

## 配置和管理员

`/etc/aicollabhub/site.json` 保存部署网络参数；`app.env` 是服务实际配置，`app-env.json` 是副本。升级读取生效 AUTH 字段，成功后同步两份文件。手工修改后需重启 API，监听端口调整则需重新生成并检查 Nginx。

网关模式的 `AUTH_PUBLIC_ORIGIN` 与 `AUTH_CALLBACK_ORIGIN` 使用同一个 HTTPS 外部 origin，`AUTH_COOKIE_SECURE=true`，平台无需安装网关证书。平台直接终止 HTTPS 则设 `tls_termination=local` 并提供对应 web_port、证书及私钥。

用户首次真实登录后，按验票返回的 workNo 授权或撤权：

```bash
sudo /opt/aicollabhub/manage.sh set-role --work-no 10001 --role admin
sudo /opt/aicollabhub/manage.sh set-role --work-no 10001 --role user
```

替换实际编号，不存在的用户会报错。启用身份同步后角色由 IAM 管理，本地命令拒绝操作。`AUTH_SESSION_SECRET` 不放入升级模板，升级不轮换；首次安装自动生成。

## 新安装及管理

新机器复制 `site.example.json` 到 root 专属配置，填写网站、专属测试库及数据目录，执行 `sudo bash ./install.sh --config /root/aih-site.json --check` 和去掉 `--check` 的命令。网关模式可追加上述网络和认证参数。现有机器不要复制模板覆盖旧库名。

默认数据库 `aicollabhub_test`，旧库名保持不变；程序 `/opt/aicollabhub`、数据 `/data/aicollabhub`。数据分区至少 10 GiB 空闲并预留备份空间，程序分区至少 3 GiB。数据目录祖先需允许服务账号遍历。PostgreSQL/Redis/MinIO 仅监听回环地址，API 默认 2 workers，数据库 60 个连接（预留 3 个）。不关闭 SELinux 或替换系统 Python。

管理命令 `sudo /opt/aicollabhub/manage.sh status|check|start|stop|restart|backup`。IAM 模式使用五个服务，模拟 IAM 停止并取消自启。凭据 `/etc/aicollabhub/credentials.json` 仅 root 可读。安装日志 `/var/log/aih-native-install.log`，检查结果 `/etc/aicollabhub/last-check.json`。

## 失败恢复

预检失败不停机；备份失败不部署新代码并恢复原运行服务。部署失败会停止服务，在 `install-state.json` 记录 `upgrade_failed` 和备份路径，保留旧版本。

恢复前核验备份 SHA256、原数据目录和旧代码目录。停服务，将失败后的数据、配置和 `/opt/aicollabhub/diagnostics` 分别移到各自分区的独立保留目录，再恢复备份；不要覆盖合并数据库目录。保留诊断目录也避免 GNU tar 恢复时因原目录非空而报错。备份含旧 current 指针、管理脚本、systemd 定义和配置。按原路径用 GNU tar `--unlink-first --same-owner` 恢复，确认命令成功后执行 `systemctl daemon-reload` 并启动检查。不得只切代码忽略数据状态。

备份不重复保存运行时和旧代码，需保留原目录和旧安装包。含凭据备份不得放入交付 ZIP；跨机恢复需匹配服务账号、运行时及属主。

## 再次打包

准备机运行 `scripts/package_native_release.py --reuse-bundle ... --output ... --release-id ... --without-loadtest`。它校验原包、重建三端及后端 wheel、复用 Linux wheelhouse、生成 ZIP 和摘要。依赖声明变化时拒绝复用。`sources`、`notices` 保留运行时源码及许可，构建工具不带到现场。
