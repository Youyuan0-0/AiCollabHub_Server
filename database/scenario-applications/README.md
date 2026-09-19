# 场景应用数据库

当前设计包含 10 张本地业务表、2 个统计视图。外部应用目录、草稿、版本和运行能力由供应方适配器提供，本地保存平台关联、开放范围、互动、审核补充项、教程文件索引与业务操作记录。

本目录只适用于当前分支的 `scene_*` 存储方案。采用 `applications`、`application_publications`、`resource_reviews` 等统一资源表的新方案，需要使用对应版本的后端代码与迁移，不能通过补建旧表代替版本对齐。执行脚本前先核对目标库的现有表结构。

| 表 | 内容 |
| --- | --- |
| `scene_application` | 外部应用关联及已生效的平台补充项 |
| `scene_visibility_target` | 可见的组织、成员和项目编号 |
| `scene_interaction` | 收藏、订阅、评分和评价 |
| `scene_submission_extension` | 发布申请的本地补充资料与规则快照 |
| `scene_activity` | 调用、下载、同步及外部写入记录 |
| `scene_review_note` | 本地七项审核评分、补充意见与审核证据；历史三项保留原值 |
| `scene_notification` | 个人消息及已读状态 |
| `scene_configuration` | 表单、展示、审核和通知配置 |
| `scene_tutorial_file` | 教程文件归属、密级、媒体类型及原文件/预览在文件服务中的位置 |
| `scene_review_report_file` | 质量评测报告的原文件、预览索引及所属申请、审核记录 |

## 文件与使用

- [schema.sql](schema.sql)：新库首次建表入口，包含约束、索引、视图和初始配置。已有业务库使用增量变更，不重复执行首次建表 SQL。
- [relations.dot](relations.dot)：可编辑关系图源文件，需查看图片时可使用 Graphviz 生成。
- [interface-evidence.md](interface-evidence.md)：供应方接口及本地字段归属依据。
- [verify-schema.cjs](verify-schema.cjs)：独立 PGlite 结构检查，通过 `NODE_PATH` 提供 `@electric-sql/pglite` 依赖，不读取项目 `.env`；结果文件由脚本生成并忽略。

场景表目前独立于框架 Alembic 迁移。启用 `APPLICATIONS_STORAGE=database` 前，须在同一 PostgreSQL 数据库中完成框架迁移及场景建表。

已有场景库增加教程上传时，执行 [add-tutorial-files-20260917.sql](add-tutorial-files-20260917.sql)。这项增量只新增附件索引表。原文件与 DOCX/文本预览写入已有 MinIO 文件服务；教程引用保存在草稿和已发布资料的 `tutorials` 本地字段中。生产部署应备份数据库和文件服务中的 `application-tutorials/` 对象。移除教程条目只解除当前表单引用，已有版本仍可读取原文件；暂不自动清理未关联上传文件。

业务入口见 [场景应用源码说明](../../app/modules/applications/README.md)。模拟外部资料使用文件快照时，必须保留 `APPLICATIONS_DEMO_DIRECTORY` 指向的目录；数据库仅保存快照引用。

已有场景库启用七项审核评分时，先执行 [review-seven-scores-20260918.sql](review-seven-scores-20260918.sql)，再重启 API。迁移保留历史三项评分和原审核门槛，新代码显式写入 `score_schema=2` 及 `quality_scores`，每项为 1–5 分整数，不设及格线。旧版服务仍可按历史格式写入，避免共享数据库的部署过渡期中断；各服务更新后才统一使用七项规则。`verify-review-migration.cjs` 可在独立 PGlite 中检查重复迁移、历史兼容及评分约束。

已有场景库支持可选审核报告时，执行 [add-review-reports-20260918.sql](add-review-reports-20260918.sql)，脚本可重复执行，不改写历史审核。报告放在现有 MinIO 存储桶的 `application-review-reports/` 目录；数据库只存元信息和关联，不存文件正文，不需要新建存储桶或公开访问策略。

报告由审核员上传，当前支持最多 3 个 PDF/DOCX/TXT/MD 文档、单个 20 MiB，这是复用现有文档处理能力的实现限制，非任务书指定。附件选填，评分和结论仍独立完成。上传后暂存并可恢复、移除；随审核提交后不可改挂或删除，后续重提保留历史报告。其他审核员不能使用本人暂存附件，但有申请管理权限的审核员可查看已提交报告。

请同时备份数据库和对应 MinIO 目录。未提交且长期不再使用的暂存文件暂不自动过期删除；可在对应申请中由上传者移除。删除索引后对象回收失败会记录清理日志，需要按文件编号人工回收，避免误删仍被引用的对象。
