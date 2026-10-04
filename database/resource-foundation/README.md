# 五大资源与身份日志数据库基线

本目录说明 2026-09-20 确认的资源基线，以及 2026-09-21 追加的文件、反馈、应用关联、模块配置、可见范围、首页问答和知识库文档审核。迁移只调整表、约束、索引与中文注释，不导入样例或旧库数据。当前后端和两套前端已使用这套结构，外部接口仍按配置模拟；未恢复此前暂存的重构。

## 表清单

测试库当前共 44 张业务表，另有 1 张 Alembic 版本记录表。2026-10-01 新增外部工具链目录快照表；四类华为资源主表、发布与文件结构保持现有约束。本地索引配置、任务、分段三表已按职责调整撤回，相关能力交由知识库工具链接口。站内通知和外部资源映射表仍未新增。

| 主表 | 发布申请表 | 正式版本表 |
| --- | --- | --- |
| `datasets` | `dataset_publications` | `dataset_versions` |
| `models` | `model_publications` | `model_versions` |
| `applications` | `application_publications` | `application_versions` |
| `knowledge_bases` | `knowledge_base_publications` | `knowledge_base_versions` |
| `ai_tools` | `ai_tool_publications` | `ai_tool_versions` |

| 共用表 | 职责 |
| --- | --- |
| `resource_ratings` | 用户对资源的当前评分及评价，每用户每资源一条 |
| `resource_favorites` | 收藏，取消时删除记录 |
| `resource_follows` | 版本更新关注，取消时删除记录 |
| `resource_downloads` | 下载请求、幂等、链接发放和有证据的完成状态 |
| `resource_reviews` | 各模块发布申请历次提交的最终审核结论和冻结快照 |
| `users` | 集团 IAM 用户最小本地映射、状态、角色及同步来源时间 |
| `auth_sessions` | 登录会话、令牌和票据摘要、过期撤销状态 |
| `iam_sync_receipts` | 成功身份同步批次的去重回执 |
| `operation_logs` | 成功业务动作，包括调用、上传和下载 |
| `system_logs` | 系统运行事件和异常，级别为 info/warning/error |
| `files` | 全平台文件元数据、存储位置和生命周期，文件本体存 MinIO |
| `feedback_records` | 原始反馈、提交幂等键及当前处理状态 |
| `feedback_events` | 不可变的提交、回复、解决、重新打开历史 |
| `feedback_attachments` | 原始反馈与已就绪文件的关联及顺序 |
| `application_dependencies` | 应用正式版本与四类资源资料版本的关联 |
| `module_configurations` | 各模块的表单、展示和流程配置及修改修订号 |
| `resource_visibility_policies` | 正式资源的全部已登录用户或指定用户可见规则 |
| `resource_visibility_targets` | 可见规则中的指定用户清单 |
| `resource_file_references` | 申请、审核、正式版本的说明文件和审核报告引用 |
| `file_previews` | 原文件与生成预览文件的关联，双方元数据复用 files |
| `conversations` | 用户会话、下一轮默认选项、历史列表和删除标记 |
| `conversation_messages` | 每条提问和回答、发送幂等、生成状态及上下文快照 |
| `conversation_message_attachments` | 用户提问与统一文件的稳定引用及展示位置 |
| `home_recommendations` | 首页热门资源和典型应用场景入口配置 |
| `home_question_suggestions` | 点击后填入输入框的推荐问题 |
| `announcements` | 公共平台公告，与个人站内消息分开 |
| `knowledge_documents` | 独立上传的知识文档及当前审核状态 |
| `knowledge_document_reviews` | 每篇文档每轮审核的不可变结论和原始文件快照 |
| `toolchain_catalog_snapshots` | 华为目录元数据快照、查询时间、失败退避和跨 worker 查询租约 |

此前讨论的资源使用行为继续由操作日志承接，下载另有 `resource_downloads`，本轮不新增未经设计的通用使用日志表。

## 发布与版本字段

以下 `resource_id` 代表具体模块的 `dataset_id`、`model_id`、`application_id`、`knowledge_base_id`、`ai_tool_id`，不是实际列名。

| 发布申请字段 | 含义 |
| --- | --- |
| `id` | UUID；驳回重提仍使用同一申请编号 |
| 各模块资源编号 | 首次发布前为空，正式发布时回填；更新申请指向既有资源 |
| `applicant_id` | 本地 `users.id`，由服务端可信身份确定 |
| `base_version_id` | 更新申请基于的正式版本，首次发布为空 |
| `version`、`version_notes` | 拟发布版本号及说明；版本号在提交时必填 |
| `data` | 本模块受校验的业务资料，JSONB 对象 |
| `review_status` | 草稿为空；pending/approved/rejected |
| `submission_no` | 草稿为 0，首次提交为 1，重提递增 |
| `revision` | 默认 1，每次保存或状态变更加 1，服务更新还须匹配旧值 |
| `submitted_at` | 最近一轮提交时间 |
| `published_version_id`、`published_at` | 正式发布的版本编号及时间 |
| `created_at`、`updated_at` | 本地申请创建和修改时间 |

各模块版本表为：`id`、对应资源编号、`publication_id`、`version`、`version_notes`、`snapshot`、`publisher_id`、`published_at`。同资源版本号唯一、来源申请唯一，正式版本禁止覆盖和删除。主表当前版本与发布申请基线通过复合外键确保属于同一资源。

审核表为：`id`、`resource_type`、`publication_id`、`submission_no`、`reviewer_id`、`decision`、`comment`、`quality_scores`、`submission_snapshot`、`submitted_at`、`created_at`。`submission_snapshot` 使用 `version`、`versionNotes`、`data` 三个基础键，允许补充非敏感的追溯资料。数据库要求这些基础键与提交内容相符，驳回意见必填；审核记录不可覆盖和删除。

审核记录写入与申请结论更新须在同一事务；正式版本创建、申请发布结果和当前主表版本应在同一事务。循环外键及结论检查延迟到事务结束，不能在每一步单独提交。首次发布的资源默认禁用，启用是独立动作。

## 共用互动字段

`resource_type` 使用 dataset/model/application/knowledge/tool。互动表均包含 UUID `id`、`resource_type`、`resource_id`、`user_id`、`created_at`。

- 收藏、关注表不额外增加状态或更新时间，用户与资源的组合唯一。
- 评价表增加 `rating`（1 至 5）、`comment`（可空）、`updated_at`，用户与资源的组合唯一。
- 下载表增加 `version_id`、`file_id`、`idempotency_key`、`status`、`error_code`、`updated_at`、`completed_at`。同一用户的幂等键唯一；状态为 pending/link_issued/completed/failed，仅 completed 必须有完成时间。
- 共用资源引用由数据库触发器校验并加行锁；下载版本须属于目标资源；有互动记录的资源不得物理删除。权限、幂等请求内容比对和可信下载完成证据仍由后端验证。
- 初始基线的 `file_id` 为可空 UUID；2026-09-21 的追加迁移已建立 `files`，补齐下载文件外键及索引、更新中文注释。外部资源映射本次明确暂缓，不创建任何共用或分模块外部映射表。

## 文件、反馈与应用关联

追加迁移版本为 `20260921_01_feedback_relations`，建立共用文件、反馈三表与应用关联共五张表。完整字段、中文说明和索引见 [反馈与应用关联迁移](../../migrations/resource_foundation/versions/20260921_01_feedback_relations.py)，跨表约束见 [对应 SQL](../../migrations/resource_foundation/versions/20260921_01_guards.sql)。

- 反馈首次创建必须同时写入提交事件；附件在提交事件之前关联，每条反馈最多三个。主表原始内容、处理事件和原始附件关联不可覆盖。处理必须把修订号加一并在同一事务记录匹配的事件；主表和事件状态不一致会拒绝提交。业务接口仍须校验管理员权限、预期修订号和相同幂等键的请求内容。
- 文件引用要求已就绪、未到期，建立引用时在同一事务清除临时期限。已就绪文件内容和对象定位不可覆盖；存在反馈或下载引用时不能清理或重新设置到期时间。数据库不执行 MinIO 操作，也不代替服务端文件权限、类型、大小和上传结果校验。
- 应用关联检查目标类型、资源及版本归属，禁止修改正式关联。应用版本快照、获批申请与结构化关联的顺序和编号在事务结束时核对；快照中的 `dependencies` 使用 `kind`、`resourceId`、`resourceVersionId`，省略依赖数组表示无关联。实际外部运行版本不由本表确定。
- 首页六表和问答附件引用保护已由第三次追加迁移补齐，业务仓储和文件清理任务仍需接入。以后增加引用关系时仍须同步扩展保护，不能只检查当时已知的引用表。

## 配置、可见范围与资源文件

追加版本为 `20260921_02_resource_support`，建立模块配置、可见规则、可见对象、资源文件引用和文件预览五张表。完整字段、中文说明和索引见 [配置与文件关系迁移](../../migrations/resource_foundation/versions/20260921_02_resource_support.py)，跨表约束见 [对应 SQL](../../migrations/resource_foundation/versions/20260921_02_guards.sql)。`notifications` 未创建。

- 配置按模块和类别唯一，JSONB 限定为对象，修订从 1 开始且修改时递增。配置字段白名单、管理员身份及保存时匹配预期修订号由服务处理，不把 JSONB 对象检查当成完整业务校验。
- 可见规则按资源唯一，类型和资源存在性受保护。指定用户不可重复，范围与名单的匹配延迟到事务结束检查：`all` 不带清单，`members` 至少一人。清单修改锁定所属规则，服务还须在同一事务匹配并递增规则修订号。组织和项目 Mock 范围未写入新表，也未将旧规则自动转换为全部可见。
- 文件引用按模块、归属、用途和文件去重；暂存报告必须对应待审轮次及实际上传人，每位审核员每轮最多三份。报告归档后不能重复使用，其他审核员或旧轮次的报告不能混入当前审核。
- 申请说明引用只能在可编辑草稿或驳回状态调整。审核先写结论记录及正式文件引用，再更新申请结论；版本先写版本及说明引用，再回填申请发布结果，均在同一事务内完成。事务提交时核对审核、版本的说明引用与申请一致，选中报告须解除暂存引用；正式历史引用不可修改、删除或事后追加。
- 模块服务仍须从受校验的 `data` / `snapshot` 提取说明文件引用；数据库不臆造所有模块的专属 JSON 结构。暂存报告上传、审核接口还须校验可信身份、管理员权限和预期申请修订号。
- 原件与预览按文件编号顺序加锁，拒绝自引用、链、环和不就绪文件；禁止把已独立使用的业务文件改成预览，或直接将预览挂到反馈、下载和资源资料上。原件在用时不允许解除预览关系，实际读取仍由服务根据原件权限控制。
- 文件清理保护现覆盖反馈附件、下载、资源文件引用及预览两端。数据库只保护记录和生命周期，暂存报告过期清理、MinIO 物理删除与重试仍需相应服务或任务实现。

## 迁移入口

新增入口复用根目录的 `alembic.ini` 和本地 `.env`，不在代码中保存数据库账号密码：

```powershell
python -m alembic -n resource_foundation upgrade head
python -m alembic -n resource_foundation current
```

最初基线版本为 `20260920_01_resource_foundation`，当前最新版本为 `20261001_01_toolchain_catalog`。未来变更继续新增迁移，不修改已经执行的文件。03 的历史建表记录保留，通过 04 明确撤回本地索引范围；当前结构以迁移链最终结果为准。

| 迁移文件 | 建设内容 | 新增业务表 |
| --- | --- | --- |
| [20260920_01_initial.py](../../migrations/resource_foundation/versions/20260920_01_initial.py) | 五类资源主表、发布与版本、共用互动与审核、认证和日志 | 25 |
| [20260921_01_feedback_relations.py](../../migrations/resource_foundation/versions/20260921_01_feedback_relations.py) | 共用文件、反馈内容与历史、应用资源关联 | 5 |
| [20260921_02_resource_support.py](../../migrations/resource_foundation/versions/20260921_02_resource_support.py) | 模块配置、可见范围、资源文件与预览关系 | 5 |
| [20260921_03_home_knowledge.py](../../migrations/resource_foundation/versions/20260921_03_home_knowledge.py) | 首页六表与知识文档、审核及索引的历史建设记录 | 11 |
| [20260921_04_external_knowledge.py](../../migrations/resource_foundation/versions/20260921_04_external_knowledge.py) | 撤回本地索引三表，保留文档、审核及文件保护 | -3 |
| [20261001_01_toolchain_catalog.py](../../migrations/resource_foundation/versions/20261001_01_toolchain_catalog.py) | 华为目录快照和查询租约，接在字段对齐迁移之后 | 1 |

首次建立基线只支持 PostgreSQL 空 schema，发现旧业务表即停止；已安装本迁移链的数据库按版本追加升级。重复升级不会重复建表。默认 `alembic upgrade head` 属于旧库迁移链，不能用于本新库；不能通过 stamp 跳过迁移或把两条链混用。降级不自动删除业务历史，回退使用操作前备份。

## 华为查询兼容

启动华为连接前执行上述 `resource_foundation upgrade head`。有数据库时缺少快照表会中止启动；无数据库的开发模式使用内存快照，重启后重新查询。

管理端“工具链接入配置”分别维护阿里和华为连接，两家可以同时启用。各模块原有 `connection` 保存阿里配置，`configuration.huawei` 按模块保存华为连接及独立工作台地址；旧单华为配置读取时迁入独立连接，不自动改写原文件。用户在“工具链发布”页面选择阿里或华为，此选择只保存在页面地址，不修改其他用户或后台配置。

华为真实模式需要服务根地址、API Key 和允许访问的平台账号 subject；后台配置保存后重启后端生效。两家同时启用的模拟示例见 [toolchain-huawei.example.json](../../config/toolchain-huawei.example.json)，其不连接真实平台。华为真实模式将 `configuration.huawei` 中对应模块的 `connection.mode` 改为 `live`，填写 `url`、`allowedSubjects`，并在 `credentials` 配置对应的 `applicationsApiKey`、`datasetsApiKey`、`knowledgeApiKey`、`modelsApiKey`。阿里继续使用自己的地址、工作空间和 AK/SK，导入来源包含供应方，外部编号相同也不会混用。

后台启动后首次查询并默认每 300 秒更新完整目录，间隔可配置为 30 至 86400 秒。多个 worker 用数据库租约协调；错误查询保留最后成功结果并退避。关闭 `pollingEnabled` 后仅在点击刷新时查询。用户页面每 30 秒检查后端快照，知识库详情按配置间隔查询文档状态。

智能体和数据集只导入可对应的元数据为发布草稿，按持久来源去重；外部版本和平台版本分开保存。模型提供者与知识库目前只支持外部目录查询。文件 ID 不作为本地 `files` 外键，模型 `config`、智能体 `engine_config` 和知识库配置不进入目录或发布资料。当前文档未交付的运行、下载、检索、上传和 PageIndex 文档接口不会被当作已接通。

## 验证与业务边界

资源基线、共用能力、首页文档和职责收缩四组测试通过 `RESOURCE_FOUNDATION_TEST_DATABASE_URL` 显式指定独立 PostgreSQL 测试库，未设置时跳过。测试不会读取项目 `.env`，也不固定某台服务器的地址。测试账号需具有创建 schema 的权限；可以使用空测试库或已有基线的独立测试库。

先通过本地环境或 CI 密钥配置上述变量，再在项目根目录执行：

```powershell
python -m pytest tests/test_resource_foundation.py tests/test_resource_support.py tests/test_home_knowledge_schema.py tests/test_knowledge_scope_migration.py -q
python -m ruff check migrations/resource_foundation tests/test_resource_foundation.py tests/test_resource_support.py tests/test_home_knowledge_schema.py tests/test_knowledge_scope_migration.py
python -m ruff format --check migrations/resource_foundation tests/test_resource_foundation.py tests/test_resource_support.py tests/test_home_knowledge_schema.py tests/test_knowledge_scope_migration.py
```

连接串使用 `postgresql+psycopg://<用户>:<密码>@<主机>:<端口>/<独立测试库>`。测试在随机 schema 和外层事务内执行迁移，搜索路径不回落到 public，逐例使用保存点，结束后整体回滚，不遗留测试用户或业务数据；异常退出路径也释放连接池。

2026-09-21 共 114 项数据库测试通过，覆盖资源发布、配置与可见范围、文件归档与清理保护、问答历史、文档审核，以及撤回迁移保留资料、遇到非空索引数据停止的场景。相关 Ruff 检查和格式检查通过。默认测试不访问远端库。

建表阶段已确认 43 张业务表为空，站内通知、外部资源映射及本地索引三表不存在。后续适配联调已产生明确标记的测试资源、会话、反馈和日志，当前测试库不再为空。迁移前备份已验证归档目录可读取并核对 SHA-256，未执行完整恢复演练。

## 首页问答与展示配置

追加版本 `20260921_03_home_knowledge` 的字段类型、中文说明与外键见 [迁移文件](../../migrations/resource_foundation/versions/20260921_03_home_knowledge.py)，发送与文件保护见 [首页约束](../../migrations/resource_foundation/versions/20260921_03_home_guards.sql)。

| 表 | 主要字段及职责 |
| --- | --- |
| `conversations` | `user_id` 归属用户；`title` 标题；`model_id`、`knowledge_base_id`、`capabilities` 为下次提问默认选项；`created_at`、`updated_at`、`deleted_at` 支撑历史列表与逻辑删除。 |
| `conversation_messages` | `conversation_id`、`sequence_no` 确定归属和顺序；`role`、`reply_to_id` 关联问答；`content`、`status` 保存正文及调用状态；`idempotency_key` 防重复发送；`context_snapshot`、`sources` 还原历史；`upstream_request_id`、`error_code`、`error_message` 供核对和用户提示；`created_at`、`updated_at` 记录时间。 |
| `conversation_message_attachments` | `message_id`、`file_id`、`position`、`created_at`；不复制文件名、大小或存储位置。 |
| `home_recommendations` | `placement` 热门资源或典型场景；`resource_type`、`resource_id` 定位资源或模块入口；`title`、`summary` 展示文案；`status` 启用或禁用；`creator_id`、创建和更新时间。 |
| `home_question_suggestions` | `question` 示例问题；`status` 启用或禁用；`creator_id`、创建和更新时间。 |
| `announcements` | `title`、`summary`、`content_markdown`；`status` 为 draft/published/withdrawn；`creator_id`、`published_at`、创建和更新时间。 |

- 六表均使用 UUID `id`。会话选项不改写已经发送的消息快照；助手必须指向同会话中更早的一条用户消息，当前每个提问最多一个回答。
- 用户消息固定为 `completed` 且有发送幂等键。助手状态为 pending/completed/failed/cancelled/unknown，其中 pending 和 unknown 每会话最多一条。unknown 不允许直接回到 pending；完成、失败和取消的结果不可覆盖。
- 保存顺序为提问、附件、助手占位，在同一短事务内完成。纯附件问题允许正文为空；事务结束时仍没有正文或有效附件则拒绝。助手占位建立后不得补入附件。接口仍须检查相同幂等键的请求内容是否一致。
- 上游调用在事务外执行，结果回写重新检查消息状态和会话删除标记；删除后不接收迟到结果。真正清理历史须先删除附件关系、回答和提问，再清理会话，不能直接绕过文件引用保护。
- 上下文使用固定字段和去重能力数组；引用来源最多 20 项、64KiB，不允许保存临时链接。快照中的资源名称和实际模型由后端根据可信资料填充；数据库不替代上游协议或资源权限校验。
- 模块入口不要求虚构资源 UUID；具体资源推荐必须存在并受删除保护。启用推荐不代表获得目标资源权限。
- 公告发布必须有正文及发布时间；已发布公告先撤回再编辑。它是公共公告，未创建个人通知、已读或消息投递表。

## 知识文档与审核

知识库名称、概览和发布版本沿用原三表；另外两表保留平台的文档上传及逐篇审批。文档分段、向量、索引配置、处理任务及检索统计交由外部知识库工具链接口负责。文档当前约束见 [文档约束](../../migrations/resource_foundation/versions/20260921_04_document_guards.sql)，审核的不可变历史约束沿用 03 迁移。

| 表 | 主要字段及职责 |
| --- | --- |
| `knowledge_documents` | `knowledge_base_id` 所属库；`file_id` 不可覆盖的原件；`name` 文档名称；`uploader_id` 上传人；`review_status`、`submission_no`、`submitted_at` 当前送审状态；`revision` 并发更新修订；`deleted_at` 逻辑删除；创建和更新时间。 |
| `knowledge_document_reviews` | `document_id`、`submission_no` 定位审核轮次；`reviewer_id`、`decision`、`comment` 保存结论；`submission_snapshot` 固化 documentId/fileId/name；`submitted_at`、`created_at` 保存提交和审核时间。 |

- 两表均使用 UUID `id`。文档通过 `files` 关联 MinIO 原件；上传人必须拥有该文件，文件必须就绪、未过期且不是预览产物。活动文档在同库内不允许名称忽略大小写后重复。
- 文档审核为 pending/approved/rejected/needs_changes，最后一项保留现有文档补充材料流程，与资源发布的三态审核分开。批准文档之前，知识库必须已有获批正式版本。审核记录与文档状态在同一事务提交，每轮只有一个不可变结论；替换原件需新建文档，不能覆盖已审核文件。
- 文档逻辑删除后不再接受修改；历史文件仍受引用保护。文件物理保留期和受控归档清理流程另行接入，不由界面删除立即删除原件。
- 本地不再保存 `processing_status`、`index_job_id`、`processed_at` 和解析 `error_message`。供应方状态、配置和结果在接口接入时按明确协议获取；需要保存哪些外部关联或同步信息另行确定，不提前复制一套引擎数据。
- 读取时仍需验证用户权限、知识库启用状态以及文档是否已审核、未删除。平台审核通过不代表外部处理完成或已经可以检索。
- 04 迁移先锁定并确认索引三表及待删除的文档运行字段未被使用，再移除相关外键、约束、函数、列与表。已有索引数据时迁移会拒绝继续，须先明确归档或迁移方式。

当前 PostgreSQL 运行装配已替换旧首页会话及 `knowledge_records` 仓储；知识库处理的开发运行态使用 Redis，不新建索引表。真实知识库接口仍待联调。个人中心联系方式只读，不增设本地暂存字段。

备份保存到执行环境指定的工程外目录；安装脚本和含环境信息的验收记录留在被忽略的 `.runtime/`，不作为其他开发人员的运行依赖。部署时重新备份并核对本环境结构，不能以本次测试库的验收结果代替目标环境检查。

业务适配由 `app/db/foundation.py`、五模块共用仓储及各业务服务完成，权限、字段校验、修订冲突、文件引用和发布原子性由接口与数据库共同约束。隔离 PostgreSQL 回归见 `tests/test_foundation_*.py`；真实外部服务和并发容量仍需独立验收。
