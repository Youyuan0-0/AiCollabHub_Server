# 首页会话 PostgreSQL 适配

已于 2026-09-15 使用本地 .env 完成真实 PostgreSQL 主备接入和会话接口验证。
前端既有新建、续聊、历史、删除、附件上传和下载接口及响应结构保持兼容。
默认回复仍由 EchoChatProvider 原样回显问题，第三方 API 可通过原有提供器接口接入。

## 配置与迁移

项目已有 psycopg[binary] 依赖；在实际后端目录安装依赖，并在本地 .env 配置：

```dotenv
DATABASE_URL=postgresql+psycopg://USER:URL_ENCODED_PASSWORD@HOST:5432/DATABASE
# 可选迁移专用账号，必须连接与 DATABASE_URL 相同的数据库及 schema。
DATABASE_MIGRATION_URL=
```

postgresql:// 和 postgres:// 也会统一选择 Psycopg 3，密码中特殊字符须百分号编码。
运行时和迁移共用驱动解析规则，既有连接池、主备选项及独立迁移账号继续有效。

也支持本地 .env 已使用的 `POSTGRES_HOST/PORT/DB/USER/PASSWORD` 与
`POSTGRES_READ_HOST/PORT/DB/USER/PASSWORD`。分项密码填写原文，程序自动编码。
完整 `DATABASE_URL` / `DATABASE_READ_URL` 非空时各自优先于对应分项配置。
程序在认证和读写分离配置校验之前组装连接串，因此现有 `AUTH_MODE=mock` 可直接启动。

迁移前备份数据库并暂停旧 API 写入，在主库执行一次：

```powershell
python -m alembic -c alembic.conversations.ini upgrade head
```

上述独立入口适用于当前共享库：已有全局版本为 20260915_02，部分其他模块迁移不在
本分支中，因此不执行全局 upgrade/stamp。会话入口只维护
`alembic_version_conversations`，当前版本为 20260915_chat01；不会改写全局版本。
空会话库或使用本分支全量迁移已建好会话表的库也可使用独立入口，已有表会先核对结构。
本分支从空库初始化的完整迁移链仍可使用默认 alembic.ini，不能套用到未知全局版本的共享库。
独立迁移完成后启动新代码，应用启动本身不会自动建表或迁移。
若迁移账号与运行账号不同，需要按既有部署方式授予运行账号新表的 SELECT、INSERT、
UPDATE、DELETE 权限；运行账号不需要建表权限。

迁移会在线读取旧数据，不能通过 --sql 生成完整离线数据搬迁脚本。
PostgreSQL 在同一迁移事务中锁定旧表，完成转换后才移除旧表；坏 JSON 会中止，
不会忽略损坏记录或用空会话替代。现有的单轮、多轮、空会话及删除标记均兼容。
迁移针对连接串指向的同一数据库，不会自动把另一份 SQLite 文件导入 PostgreSQL。

独立入口降级至 base（完整链为 20260914_03）会将当前关系表重新打包为旧版 JSON，包含升级后新增的消息，
然后移除新表；不是恢复过期的数据快照。回退也应暂停 API 并切换回匹配的旧代码。

## 数据结构与事务

| 表 | 内容 |
| --- | --- |
| conversations | ID、标题、模型、知识库、工具、owner_id、创建/更新时间、version、删除标记 |
| conversation_messages | 会话 ID、顺序号、消息 ID、角色、正文、时间、模型、知识库、工具、引用、附件元信息 |

PostgreSQL 使用 TIMESTAMP WITH TIME ZONE 和 JSONB；消息按 position 排列，
保留旧消息 ID，不用时间戳推断先后。消息外键引用会话，复合主键禁止同会话顺序号重复。
历史列表利用会话索引在数据库中按所属用户/共享记录过滤、倒序排列并限制 100 条，
不读取所有消息正文。详情以单条关联查询获得一致的会话版本与完整消息。

追加一轮前比较 version 并条件更新会话行，然后在同一事务中插入用户与助手消息；
并发覆盖、修改既有消息或删除后的迟到结果返回 409。第三方提供器执行期间不持有数据库事务。
删除会清除数据库消息及摘要，保留空删除标记，避免固定样例重新出现。
会话查询和写入继续使用主库连接，发送成功后的历史重读不会分流到可能延迟的备库。

owner_id 保留既有可信身份绑定规则；旧无主记录和三条文件固定样例仍按共享演示处理。
本次并未开放生产演示端点，也未改变登录或 CSRF 行为。

## 附件与验证边界

消息的 attachments 保存附件编号、名称、大小及 stored 状态，迁移保留这些字段。
原文件及上传仓库索引仍使用既有 LocalAttachmentStore，需要继续保留
CONVERSATION_ATTACHMENT_DIR 指向的目录；数据库迁移不会搬迁原文件。
删除会话后原文件无法通过 API 下载，但物理清理策略沿用现有实现，参见 ../ATTACHMENTS.md。

本地针对性检查使用临时 SQLite 验证完整接口、用户隔离、附件历史、迁移升降级、
删除及冲突回滚，并用 PostgreSQL 方言编译检查 JSONB、时区类型与约束。

真实库另已通过 16 项检查：主库、备库、独立迁移、全局版本保留、原生字段类型、新建、
两轮原样回显、历史列表、重建应用后的消息持久化、附件下载、备库可见性、
两个并发连接的版本冲突、冲突后消息完整性、删除、消息删除及测试数据清理。
迁移前旧会话表有 0 条记录，未丢失既有会话；测试结束只清理本次随机会话和临时附件。

```powershell
# 已迁移的数据库可直接复验；不修改部署 .env，不占用用户的服务端口。
python -m scripts.verify_conversations_postgresql --env .env
# 尚未迁移时显式选择 --migrate，脚本先备份旧会话，再执行独立迁移。
python -m scripts.verify_conversations_postgresql --env .env --migrate
```

脚本默认将脱敏结果写入 .runtime/conversations-postgresql.json；迁移备份位于同目录的
*.legacy-backup.json。接口测试在独立应用实例中关闭认证以专测数据库链路，
部署的 AUTH_MODE、CSRF 和登录配置保持不变，因此本次不作为真实登录端到端验收。

