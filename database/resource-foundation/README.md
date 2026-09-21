# 五大资源与身份日志数据库基线

本目录说明 2026-09-20 确认的资源基线，以及 2026-09-21 追加的文件、反馈、应用关联、模块配置、可见范围及资源文件关系。迁移只建表、约束、索引与中文注释，不导入 Mock 资源或旧开发库业务数据，不切换现有前后端业务实现，也不恢复此前暂存的重构。

## 表清单

测试库当前共 35 张业务表，另有 1 张 Alembic 版本记录表。最初基线 25 张，两次追加各 5 张。站内通知和外部资源映射仍按用户要求暂缓建表。

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
- 首页问答、其他六张首页表及文件清理任务尚未接入。资源文件与预览引用已由下一次迁移扩展保护；以后新增问答消息附件时仍需一起扩展，不能只检查当时已知的引用表。

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

最初基线版本为 `20260920_01_resource_foundation`，当前最新版本为 `20260921_02_resource_support`。每次 Python 迁移及对应 SQL 文件冻结该次表结构与约束。未来变更继续新增迁移，不修改已经执行的文件。

| 迁移文件 | 建设内容 | 新增业务表 |
| --- | --- | --- |
| [20260920_01_initial.py](../../migrations/resource_foundation/versions/20260920_01_initial.py) | 五类资源主表、发布与版本、共用互动与审核、认证和日志 | 25 |
| [20260921_01_feedback_relations.py](../../migrations/resource_foundation/versions/20260921_01_feedback_relations.py) | 共用文件、反馈内容与历史、应用资源关联 | 5 |
| [20260921_02_resource_support.py](../../migrations/resource_foundation/versions/20260921_02_resource_support.py) | 模块配置、可见范围、资源文件与预览关系 | 5 |

首次建立基线只支持 PostgreSQL 空 schema，发现旧业务表即停止；已安装本迁移链的数据库按版本追加升级。重复升级不会重复建表。默认 `alembic upgrade head` 属于旧库迁移链，不能用于本新库；不能通过 stamp 跳过迁移或把两条链混用。降级不自动删除业务历史，回退使用操作前备份。

## 验证与业务边界

`tests/test_resource_foundation.py` 和 `tests/test_resource_support.py` 通过 `RESOURCE_FOUNDATION_TEST_DATABASE_URL` 显式指定独立 PostgreSQL 测试库，未设置时跳过。测试不会读取项目 `.env`，也不固定某台服务器的地址。测试账号需具有创建 schema 的权限；可以使用空测试库或已有基线的独立测试库。

先通过本地环境或 CI 密钥配置上述变量，再在项目根目录执行：

```powershell
python -m pytest tests/test_resource_foundation.py tests/test_resource_support.py -q
python -m ruff check migrations/resource_foundation tests/test_resource_foundation.py tests/test_resource_support.py
python -m ruff format --check migrations/resource_foundation tests/test_resource_foundation.py tests/test_resource_support.py
```

连接串使用 `postgresql+psycopg://<用户>:<密码>@<主机>:<端口>/<独立测试库>`。测试在随机 schema 和外层事务内执行迁移，搜索路径不回落到 public，逐例使用保存点，结束后整体回滚，不遗留测试用户或业务数据；异常退出路径也释放连接池。

2026-09-21 共 89 项数据库测试通过，包含原有 61 项，以及配置修订冲突、五类资源可见范围、审核和版本文件归档、报告轮次和审核员隔离、历史文件保护、预览链和文件清理限制。相关 Ruff 检查和格式检查通过。默认测试不访问远端库。

测试库已实测升级至最新版本：35 张业务表的记录数均为 0，站内通知和外部资源映射表不存在。已通过独立连接确认提交成功、原表行数不变且没有遗留测试 schema。迁移前备份已验证归档目录可读取并核对 SHA-256，未执行完整恢复演练。

备份保存到执行环境指定的工程外目录；安装脚本和含环境信息的验收记录留在被忽略的 `.runtime/`，不作为其他开发人员的运行依赖。部署时重新备份并核对本环境结构，不能以本次测试库的验收结果代替目标环境检查。

本轮完成结构建设。新资源表的 ORM、接口和前后端流程对齐仍属后续任务；管理员准入、模块 JSON 白名单、内容与文件大小限制、密级权限、发布前锁定资源并检查 `base_version_id`、完整概览与版本同步等业务规则需要由对应服务落实。数据库建好不等于这些模块已接入真实业务。
