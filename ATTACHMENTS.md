# 首页对话附件（开发阶段）

本阶段打通选择附件、粘贴文件/截图、拖拽文件到输入区、发送时上传、关联消息、历史展示和下载。三种添加方式复用相同上传接口和校验，普通文字粘贴保持原生行为，文件夹不支持。附件状态 `stored` 仅表示原文件已保存；不包含文档解析、OCR、病毒扫描或模型对文件内容的理解，当前回复提供器仍默认回显问题。仍需输入问题文字才能发送。

## 存储与后续迁移

图片附件支持发送前缩略图、点击放大和历史图片预览（PNG、JPEG、GIF、WebP）。历史图片复用已鉴权的原文件下载接口，前端按实际文件头设置图片类型并交给浏览器解码；仅在接近可见区域时加载。预览失败可以重试或下载原文件，移除附件、切换会话和卸载页面时取消请求并释放预览资源。图片预览不代表模型已识别图片内容。

- 保留现有临时 `ConversationStore` 和会话 JSON，不新增正式业务表、不执行新的数据库迁移。
- 原文件和附件元信息由独立 `AttachmentStore` 管理，默认适配器为 `LocalAttachmentStore`。文件内容不写入会话 JSON；消息只保存附件展示信息和编号。
- 默认目录为项目下 `.runtime/attachments`（已被 Git 忽略），可通过 `CONVERSATION_ATTACHMENT_DIR` 指定绝对路径。目录按首次上传创建，重启后保留。
- 同一主机多个 API 进程需要使用同一目录；不同主机部署必须使用共享目录，或先实现共享仓库适配器。当前上传不依赖 MinIO 配置，不会在 MinIO 不可用时隐式切换数据源。
- 后续升级正式 PostgreSQL 会话/消息表时替换 `ConversationStore`；附件元信息可迁移 PostgreSQL，原文件可迁移 MinIO，只需实现并注入 `AttachmentStore`，保留会话 ID、附件 ID 和前端契约。
- 上传成功但消息失败时保留原文件供重试。删除会话会立即阻止附件下载，但本阶段不物理删除附件；未发送及已删除会话的文件清理、配额和保留策略留待正式存储阶段。临时目录需与开发数据一起管理，不应单独清空后继续保留历史。

## 接口

路径统一带 `/api/v1` 前缀，业务错误保留稳定 `detail` 错误码。

| 方法与路径 | 输入与行为 |
| --- | --- |
| `POST /conversations/{id}/attachments` | multipart 的单个 `file`，成功返回 201：`{id, name, size, status: "stored"}` |
| `POST /conversations/{id}/messages` | 现有 JSON 增加可选 `attachmentIds: string[]`，必须全部属于当前会话 |
| `GET /conversations/{id}/messages` | 用户消息增加 `attachments`，旧记录缺省为 `[]` |
| `GET /conversations/{id}/attachments/{attachmentId}/content` | 校验会话权限后强制下载原文件，UTF-8 文件名，`nosniff` |

每轮最多 10 个附件，单个不超过 10 MiB，合计不超过 20 MiB。上传使用单文件请求，现有 Nginx `client_max_body_size 11m` 可容纳文件和 multipart 开销。服务端还在解析期间限制请求体，逐块检查文件真实大小。允许 txt、md、csv、pdf、doc/docx、ppt/pptx、xls/xlsx、png、jpg/jpeg、gif、webp；扩展名白名单只限定存储范围，不代表已验证或解析文件内容。

开启登录后新对话绑定可信身份，仅所属用户可以读取、发送、上传、下载和删除；所有写请求继续校验 CSRF。旧无主共享历史保持原有文字交互，但不能上传私人附件，需要新建对话。显式关闭认证的开发环境仍共享历史。生产环境继续拒绝这些开发接口。

旧 `POST /conversations` multipart 兼容入口保持原行为，不用于新增附件链路。首页先创建空会话，再逐个上传原文件，最后以编号发送消息；部分上传失败只重传失败文件。消息提交结果不确定时先读取历史核对，不自动重发。

## 验证

后端运行 `python -m pytest tests/test_conversations.py tests/test_home_auth_integration.py`。前端运行 `npm run typecheck`、`npm run test:run` 和 `npm run build`。浏览器需验证桌面与移动端的选择、移除、上传提示、历史恢复、原文件下载，以及切换会话时取消迟到结果。
