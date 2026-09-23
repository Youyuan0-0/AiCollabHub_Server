# 麒麟 V10 SP3 原生离线安装与更新

本次使用 ZIP 交付，解压后直接得到安装文件。包含 Python 3.13.15、PostgreSQL 17.11、Redis 7.4.11、MinIO RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772、Nginx 1.30.5、全部 Python 运行依赖，以及最新服务端、用户端和管理端。目标机不需要 Docker、Node.js、编译器，也无需联网下载依赖。

适用基线：麒麟高级服务器 V10 SP3 2403、x86_64、glibc 2.28 及以上、systemd。其他 CPU 架构、麒麟补丁版本或安全加固策略需另行验证；脚本不会关闭 SELinux 或替换系统 Python/glibc。

## 测试库与正式库

本包明确用于测试与部署演练：`profile=rehearsal`、`database_environment=test`，后端使用 `APP_ENV=test`，认证为模拟 IAM，模型执行为 simulation。

- 全新安装：默认创建本机测试库 `aicollabhub_test`；可在模板的 `postgres_db` 中填写其他专属测试库名。
- 更新旧环境：复用安装时保存的库名。R1 的本地测试库虽然叫 `aicollabhub`，更新也会保留这个名字、现有数据和凭据，不会换成空库。
- 本包不连接开发机 `.env` 中的云端 15434 测试库，也不包含云端数据库备份、连接密码或本地 `.env`。
- 后续改正式环境，需要独立完成正式库、真实 IAM、模型服务和业务验收；不能只把 `test` 字符串改成 `production`。本包会拒绝正式环境配置。

## 导入 ZIP

先将 ZIP 和同名 `.sha256` 文件一起通过甲方允许的介质导入服务器。例如放到当前用户的 `~/packages` 目录，在麒麟终端执行：

```bash
cd ~/packages
sha256sum --check AiCollabHub-KylinV10SP3-x86_64-20260922-native-r2.zip.sha256
unzip AiCollabHub-KylinV10SP3-x86_64-20260922-native-r2.zip -d ~/aih-packages
cd ~/aih-packages/AiCollabHub-KylinV10SP3-x86_64-20260922-native-r2
```

`cd` 切换目录；`sha256sum` 检查传输是否损坏；`unzip` 解压，不会安装软件。以后发布编号以实际文件名为准。不要把新包解压覆盖到旧包目录。

如果没有 `unzip`，且系统有 Python 3，可替换解压命令为 `python3 -m zipfile -e 文件名.zip ~/aih-packages`。也可先在 Windows 解压，再完整导入解压后的目录；安装不依赖系统 Python。仍需刻光盘时，将解压后的全部文件刻录为数据光盘，再只读挂载使用。ZIP 本身不是启动镜像。

## 更新已经安装的环境

R2 支持从 `20260922-native-r1` 更新，或对已完成的 R2 重复更新。首先进入上面的解压目录，执行：

```bash
sudo bash ./update.sh --check
sudo bash ./update.sh
sudo /opt/aicollabhub/manage.sh check
```

第一条检查文件校验和、麒麟版本、原部署配置、原测试库及迁移版本，不停止服务。第二条读取 `/etc/aicollabhub/site.json`，自动备份后更新。第三条复核结果。更新不需要重新复制配置模板，否则可能误改旧库名。

更新过程：检查兼容性 → 暂停六个服务 → 备份数据库、文件、缓存、凭据和部署配置 → 离线部署新代码 → 迁移现有数据库 → 启动服务 → 验证网站与读写。网站会短暂停机。备份保存在数据目录同级的 `aicollabhub-backups`，例如 `/data/aicollabhub-backups`，同时输出 `.sha256`。

本次新增的数据库迁移为 `20260922_01_review_needs_changes`，在资源审核中增加“退回修改”结果；保留原有拒绝记录。使用的唯一迁移入口为 `alembic -n resource_foundation upgrade head`。不要运行旧链 `alembic upgrade head`，不要删除表或通过 stamp 跳过迁移。

Python、PostgreSQL、Redis、MinIO、Nginx 版本未变，更新复用已安装运行时；每个代码版本有独立 Python 环境和前端目录。旧代码版本、库名、数据、密码保留。更新时不允许更改地址、端口、数据库名或数据目录，也不支持跨运行时版本、未知来源版本或降级。

## 全新安装

只有尚未安装本套服务的机器才复制模板：

```bash
sudo cp ./site.example.json /root/aih-site.json
sudo vi /root/aih-site.json
sudo bash ./install.sh --config /root/aih-site.json --check
sudo bash ./install.sh --config /root/aih-site.json
```

`cp` 复制配置模板；`vi` 编辑现场地址；带 `--check` 的命令只预检；最后一条完成安装。

需要确认的配置：

- `public_origin`：客户端实际访问地址，例如 `http://10.10.20.30`。
- `data_dir`：默认 `/data/aicollabhub`，放在甲方安排的数据磁盘；末级目录保留 `aicollabhub`。至少 10 GiB 空闲，另留备份空间；程序分区至少 3 GiB 空闲。
- `web_port`：默认 80；非 80 时同步修改 `public_origin`，例如 `http://10.10.20.30:8080`。两个网站共用入口，用户端 `/`、管理端 `/admin/`。
- `postgres_db`：默认 `aicollabhub_test`。其他内部服务端口保持默认或改为空闲端口；它们仅监听本机。
- `api_workers`：默认 2，可选 1—4；数据库最多 60 个连接，其中超级用户预留 3 个。
- `open_firewall`：默认 true，仅在 firewalld 运行时放行网站端口。其他网络访问控制还需放行客户端到该端口。
- HTTPS：填写 `https://实际域名`、`web_port=443`，并将已导入服务器的证书和私钥绝对路径填入 `tls_certificate`、`tls_private_key`。
- `profile`、`database_environment`：保持 `rehearsal`、`test`。密码由首次安装随机生成；重装和更新复用原密码。

安装脚本拒绝接管未知数据目录和已有服务，不自动删除数据。同版本、同配置的 `install.sh` 可重新执行，但不同版本必须走 `update.sh`。

## 管理与存储位置

```bash
sudo /opt/aicollabhub/manage.sh status
sudo /opt/aicollabhub/manage.sh check
sudo /opt/aicollabhub/manage.sh restart
sudo /opt/aicollabhub/manage.sh backup
```

`status` 显示六个服务；`check` 检查精确迁移版本、业务表、账号权限、Redis/MinIO 读写、网站静态资源和 API；`restart` 重启本套服务；`backup` 停机备份后恢复原来运行的服务。另支持 `start`、`stop`。

服务名：`aih-postgres`、`aih-redis`、`aih-minio`、`aih-iam`、`aih-api`、`aih-web`。查看 API 日志可用 `sudo journalctl -u aih-api -n 100 --no-pager`，用于排查最近 100 条后端日志。

程序：`/opt/aicollabhub`；当前版本指针：`/opt/aicollabhub/current`；配置：`/etc/aicollabhub/site.json`；凭据：`/etc/aicollabhub/credentials.json`（仅 root 可读）；安装日志：`/var/log/aih-native-install.log`；验收结果：`/etc/aicollabhub/last-check.json`。备份包含凭据，应按数据库备份保管，不得放入交付 ZIP。

## 更新失败与恢复

如果更新失败，脚本停止服务，保留旧代码和更新前备份，在 `/etc/aicollabhub/install-state.json` 记录 `upgrade_failed` 与 `backup` 路径。排查前不要手动启动新旧混合服务，也不要只切换前端或代码指针来代替数据库恢复。

同机恢复时，应先验证备份 SHA256，确认备份内记录的原版本及原数据目录，确认 `/opt/aicollabhub/releases/原版本` 仍在。停止六个服务后，将失败后的业务数据目录和 `/etc/aicollabhub` 分别移动到独立保留目录，再将备份按原绝对路径恢复。不要直接把数据库文件覆盖合并到现有目录，因为新文件会残留。备份同时包含旧 `current` 指针、管理脚本、配置与 systemd 服务定义；使用 GNU tar 的 `--unlink-first --same-owner` 恢复并执行 `systemctl daemon-reload`，再启动和检查。实际恢复会覆盖运行状态，需核对具体路径后操作。

运行时没有变化，因此备份不重复保存运行时和旧代码；必须保留旧版本目录及同版本安装包。跨机器恢复还需要重新创建服务账号、匹配文件属主、安装对应运行时；本流程不等于跨机器自动恢复工具。

## 版本与再次打包

`manifest.json` 记录三端提交、软件版本、数据库用途、精确迁移版本和受支持升级来源。`SHA256SUMS` 校验每个文件；`sources`、`notices` 保留基础软件源码、来源和许可记录。

维护脚本位于仓库 `deploy/native`，准备机打包入口为 `scripts/package_native_release.py`。该入口验证旧包完整性，重新打包受版本控制的代码和迁移，重建后端 wheel，复用与依赖声明一致的 Linux wheelhouse，生成 ZIP 和摘要。构建前需在准备机装好 hatchling，及两个前端的构建依赖；这些工具不需要装到甲方服务器。若项目依赖声明变化，必须先在同版本麒麟构建机更新离线 wheelhouse；脚本会拒绝继续复用不匹配依赖。
