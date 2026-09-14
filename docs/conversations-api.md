# 开发问答与历史接口

主页对话及历史默认通过 HTTP 访问后端，固定内容位于 `app/data/conversations_demo.json`。
新提交问题的 `text` 原样等于 `question`，保留空格和换行，`sources` 为空数组；
模型和知识库选择项继续回显，不调用真实模型或检索。新结果保存到历史，已有历史内容不改写。

| 方法与路径 | 请求 / 返回 |
| --- | --- |
| POST `/api/v1/conversations` | multipart `input` JSON 字符串与可选 `files`；返回 ChatAnswer |
| GET `/api/v1/conversations` | 返回 ConversationSummary 数组，最新记录在前，最多 100 条 |
| GET `/api/v1/conversations/{id}` | 返回 ChatAnswer；不存在或已删除为 404 |
| DELETE `/api/v1/conversations/{id}` | 删除开发历史，成功 204 |
| POST `/api/v1/conversations/new` | JSON `{}` 或初始选项，新建空对话，返回 201 和完整会话 |
| GET `/api/v1/conversations/{id}/messages` | 获取完整会话及按顺序排列的用户、助手消息 |
| POST `/api/v1/conversations/{id}/messages` | JSON ChatInput，向已有对话追加一轮，返回最近一轮 ChatAnswer |

`input` 示例：

```json
{"question":"航空发动机常见故障有哪些？","model":"AeroLLM-LongCat-2.0","knowledgeBase":"航空发动机技术文档","tools":[]}
```

ChatAnswer 字段为 `id/question/model/knowledgeBase/text/sources`；历史摘要为
`id/title/time/model`，沿用前端已有契约。问题必须包含非空白字符，最长 10000 字符。
表单最多接收 10 个附件，解析后检查总量不超过 20 MiB，附件不保存、不解析、不参与回答；
实际部署还应由网关限制整个上传请求体大小。

默认提供三条固定历史；新问答保存在独立 `demo_conversations` 表，刷新或重启后保留。
同一对话中的消息保存在一个完整 JSON 内，兼容旧版单轮回答记录，无需新增迁移。
删除固定历史会保存删除标记，刷新后不会重新出现。当前为共享开发记录，没有用户身份、
登录或用户隔离，只用于本机联调。它与未来正式会话表分离，不代表个人会话服务已完成。

仅 `APP_ENV=development/test` 可匿名访问这些演示端点；生产统一返回
503 `conversations_not_configured`，待第三方 SSO 和真实对话服务接入。
该范围不改变看板 PUT 或其他接口鉴权。

在实际启动后端的目录配置数据库并执行：

```powershell
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

新增迁移为 `20260914_02`，接续看板迁移；不要对旧的、不匹配迁移链的 `development.db`
直接 stamp。推荐继续使用独立 `overview-integration.db`。

```powershell
curl.exe http://127.0.0.1:8000/api/v1/conversations
# 请求体文件使用 UTF-8 JSON，避免 Windows 终端引号转义问题。
curl.exe -X POST http://127.0.0.1:8000/api/v1/conversations -F "input=<question.json"
```

前端开发 HTTP 模式的 `VITE_API_SCOPE=overview` 包含看板与问答历史；
资源目录等未接入模块及其会话仍使用原有前端 Mock，页面守卫继续检查会话及权限，
演示身份不授予任何后端权限。只配置 `VITE_API_MODE=http` 而省略范围时保持全站 HTTP；
显式 `VITE_API_MODE=mock` 保留完整前端演示。
问答接口失败会显示错误，不回退到前端模拟历史，也不清除独立的前端演示会话。

## 临时会话存储与第三方 API 扩展

`ConversationService` 管理新建、发送、读取和删除，依赖两个可替换接口：

- `ConversationStore`：存储协议，当前 `SqlConversationStore` 复用临时数据库表。
  后续可用正式会话表、消息表实现同一协议，业务层无需直接操作新表。
- `ChatProvider`：回复协议，当前 `EchoChatProvider` 原样回显问题。
  后续通过 `create_app(chat_provider=...)` 注入第三方实现，适配器的 `reply` 接收
  完整历史及当前问题、模型、知识库和工具，返回 `ChatReply(text, sources)`。
  当前为同步接口，路由在线程池执行；供应方适配器应自行配置超时及服务端凭据。

新建请求可以为 `{}`，也可指定 `title/model/knowledgeBase/tools`。
返回的完整会话字段为 `id/title/model/knowledgeBase/tools/createdAt/updatedAt/version/messages`。
每条消息包含 `id/role/content/createdAt/model/knowledgeBase/tools/sources`。
用户消息和助手回复原子保存，第三方调用期间不持有数据库事务。
回复失败返回 502 `chat_provider_unavailable`，保留已有历史，不保存半轮消息。
版本冲突或回复期间会话被删除返回 409 `conversation_write_conflict`，不覆盖较新的记录。
每个对话暂限 200 条消息（100 轮），超过后返回 409 `conversation_message_limit`。

旧 `POST /conversations` 仍兼容前端 multipart 请求，自动新建对话并保存第一轮。
旧 `GET /conversations/{id}` 返回最近一轮，空对话返回空问题与空回答。
新增 `/messages` POST 使用 JSON，不接受文件；旧提交接口附件处理方式不变。
前端“新建”按钮已接入 `/new`，空对话立即保存并出现在历史中；后续问题向同一 ID 的
`/messages` 追加。选择历史时读取完整消息列表，删除当前会话只清空页面状态，不自动新建。
桌面常驻历史栏会自动加载，窄屏按打开状态加载；加载提示依据实际请求状态显示。
当前多轮 JSON 接口不处理附件，前端保留附件并明确提示移除后再发送，避免静默丢弃。

示例流程：

```text
POST /api/v1/conversations/new
{}

POST /api/v1/conversations/{返回的id}/messages
{"question":"第一问","model":"演示模型","knowledgeBase":"","tools":[]}

GET /api/v1/conversations/{返回的id}/messages
DELETE /api/v1/conversations/{返回的id}
```

仍为共享开发历史，生产演示限制不变。正式用户会话需在第三方 SSO 接入后补充用户归属、
访问控制及独立存储设计，不能把当前临时共享记录直接作为个人会话服务。

数据库未迁移或不可用返回 503 `conversations_unavailable`；记录损坏返回
503 `conversation_record_invalid`；输入错误为 422；附件总量超限为 413。
