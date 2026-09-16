# 场景应用数据库

当前设计包含 8 张本地业务表、2 个统计视图。外部应用目录、草稿、版本和运行能力由供应方适配器提供，本地保存平台关联、开放范围、互动、审核补充项与业务操作记录。

| 表 | 内容 |
| --- | --- |
| `scene_application` | 外部应用关联及已生效的平台补充项 |
| `scene_visibility_target` | 可见的组织、成员和项目编号 |
| `scene_interaction` | 收藏、订阅、评分和评价 |
| `scene_submission_extension` | 发布申请的本地补充资料与规则快照 |
| `scene_activity` | 调用、下载、同步及外部写入记录 |
| `scene_review_note` | 本地评分、补充意见与审核证据 |
| `scene_notification` | 个人消息及已读状态 |
| `scene_configuration` | 表单、展示、审核和通知配置 |

## 文件与使用

- [schema.sql](schema.sql)：新库首次建表入口，包含约束、索引、视图和初始配置。已有业务库使用增量变更，不重复执行首次建表 SQL。
- [relations.dot](relations.dot)：可编辑关系图源文件，需查看图片时可使用 Graphviz 生成。
- [interface-evidence.md](interface-evidence.md)：供应方接口及本地字段归属依据。
- [verify-schema.cjs](verify-schema.cjs)：独立 PGlite 结构检查，通过 `NODE_PATH` 提供 `@electric-sql/pglite` 依赖，不读取项目 `.env`；结果文件由脚本生成并忽略。

场景表目前独立于框架 Alembic 迁移。启用 `APPLICATIONS_STORAGE=database` 前，须在同一 PostgreSQL 数据库中完成框架迁移及场景建表。

业务入口见 [场景应用源码说明](../../app/modules/applications/README.md)。模拟外部资料使用文件快照时，必须保留 `APPLICATIONS_DEMO_DIRECTORY` 指向的目录；数据库仅保存快照引用。
