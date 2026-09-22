# 首页会话 PostgreSQL 适配

首页使用新基线的 `conversations`、`conversation_messages`、`conversation_message_attachments` 和共用 `files`。原文件存 MinIO，用户归属关联 `users.id`。既有新建、续聊、历史、删除及附件接口保持兼容，默认回复仍由 `EchoChatProvider` 回显。

## 配置与迁移

在本地 `.env` 配置 PostgreSQL、Redis、MinIO 和模拟认证，数据库必须是已确认的新结构测试库：

```powershell
python -m alembic -n resource_foundation current
python -m alembic -n resource_foundation upgrade head
python -m scripts.verify_services
```

迁移每次发布单独执行，不在 worker 启动时建表。新链不能原地转换旧会话表；不要对新库执行默认旧迁移或 `alembic.conversations.ini`。本次没有导入旧库、旧共享样例或 `.runtime/attachments` 中的旧文件。

## 事务与状态

| 表 | 职责 |
| --- | --- |
| `conversations` | 用户 UUID、标题、下次提问的模型和知识库选项、能力列表、创建/更新时间、删除标记 |
| `conversation_messages` | 顺序号、角色、提问关联、正文、生成状态、发送幂等键、上下文与来源快照、受控错误信息 |
| `conversation_message_attachments` | 提问消息与文件 UUID 的有序关联 |
| `files` | 上传人、文件名、摘要、大小、Bucket、对象键、就绪及清理状态 |

发送先锁会话，校验资源和附件归属，以短事务插入用户提问及助手占位。每个会话最多一条处理中回答；外部调用在事务之外执行，回写时再次检查状态和删除标记。完成、失败、取消后的消息不会被迟到响应覆盖。

前端每次新发送生成 `idempotencyKey`，不确定结果先重新读取历史。重复键且内容相同复用原回答，内容不同返回 409；即使之后已有其他问答，也不会错误地返回最新一轮答案。未传键的旧调用仍可发送，但不具备跨请求去重保障。

助手状态为 pending/completed/failed/cancelled/unknown。失败保留提问和错误状态；进程中断超过 15 分钟的占位在读取时标记为失败。删除会话使用逻辑标记，阻止后续读取、上传和结果回写，保留历史引用，不直接物理删除文件。

## 附件

上传时先校验会话权限，将原件保存至 MinIO 并登记 `files`。发送时只接受本用户、本会话的就绪附件，在同一事务中绑定消息并解除临时过期标记。不能用其他会话的编号借用附件；发送失败回滚消息与关联。

未使用的临时文件可由统一文件清理任务清理；已进入消息历史的文件受引用保护。附件目前只展示和下载，不解析、不参与模型回答。备份和恢复须同时覆盖 PostgreSQL 与 MinIO。

## 验证

安装开发依赖后可执行：

```powershell
python -m scripts.verify_conversations_postgresql --env .env
```

脚本只在 development/test 环境运行新仓储回归，创建随机隔离 schema，测试结束整体回滚，不新增 public 会话、不迁移目标库、不修改部署认证。覆盖重建仓储后的历史、幂等、附件归属、文件保护、失败状态和迟到响应。数据库账号须允许创建 schema；这不替代真实 IAM、MinIO 服务或容量验收。
