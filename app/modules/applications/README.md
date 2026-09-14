# 场景应用开发说明

场景应用业务通过平台后端贯通User与Admin。当前使用模拟供应方和内存仓储。完成范围见 [需求核对](./requirements-check.md)，后续数据库与实际接口见 [接入交接](./integration-handoff.md)。

## 用户端页面

- `/applications`：紧凑目录、搜索、分类、排序、详情、收藏。
- `/applications/publish`、`/applications/publications`、`/applications/publications/{id}`：资料、预览、草稿、提交、重提及进度。
- `/applications/toolchain`：外部目录、版本、导入、同步结果和重试。
- `/applications/toolchain/workspace`：本地模拟工具链定义发布。配置外部开发地址后入口跳到供应方。
- `/applications/{id}/experience`：输入/配置、模拟结果、失败重试和历史；详情同时提供订阅、评分、分享和资料下载。
- `/applications/activity`：订阅、个人调用记录、消息及已读状态。

## 管理端页面

`/applications` 已上架列表；`/applications/detail/{id}` 完整资料、版本、统计、开放范围；`/applications/reviews` 审核队列；`/applications/reviews/{id}` 资料、校验报告、打分和处理；`/applications/audit` 审核历史、同步及运行通知；`/applications/configuration` 表单/管理列；`/applications/policy` 展示、审核规则、通知和工具链入口。

## API（前缀 /api/v1）

| 方法与路径 | 行为 |
| --- | --- |
| GET `/applications`、`/applications/{id}` | 公开目录和完整详情，按身份/范围过滤 |
| GET `/application-dependencies` | 四类关联资源目录 |
| POST `/applications/{id}/version-publications` | 创建新版本草稿；已有未完成申请时沿用 |
| GET/POST `/application-publications`；GET/PUT `/{id}` | 自己可管理的发布申请；草稿/提交/重提 |
| GET `/application-session`、`/admin/application-session` | 当前可信或开发身份 |
| POST `/application-demo-session` | 仅AUTH_MODE=disabled可切预置身份 |
| GET/PUT `/applications/{id}/interaction`；GET `/applications/{id}/ratings` | 收藏/订阅/评分、评价列表 |
| POST `/applications/{id}/downloads` | 导出资料JSON并计数 |
| POST `/applications/{id}/runs` | 校验输入和配置，幂等运行并记录结果 |
| GET `/application-activity`；PUT `/application-notifications/{id}/read` | 个人订阅、运行、通知与已读 |
| GET `/application-toolchain/resources` | 模拟外部目录及版本 |
| GET/POST `/application-toolchain/syncs` | 同步历史/导入，保留失败与映射 |
| POST `/application-toolchain/development-publications` | 演练发布到模拟工具链目录 |
| GET `/admin/applications`、`/admin/applications/{id}` | 管理目录与统计 |
| PUT `/admin/applications/{id}/visibility` | 全部/组织/成员/项目范围，仅管理员 |
| GET `/admin/application-publications`、`/{id}` | 审核资料 |
| GET `/admin/application-review-queue` | 状态/关键词/分类/日期/优先级/提交时间及分页 |
| GET `/admin/application-publications/{id}/quality` | 资料规则检查与补充建议 |
| POST `/admin/application-publications/{id}/review` | 通过、驳回或需补充材料 |
| GET `/admin/application-review-history` | 按审核人/结论/日期/关键词查历史 |
| GET `/admin/application-syncs`、`/admin/application-notifications` | 同步追溯与运行异常通知 |
| GET `/application-form-configuration`；GET/PUT `/admin/application-configuration` | 字段必填/提示与管理列顺序 |
| GET `/application-presentation`；GET/PUT `/admin/application-policy` | 广场展示配置及审核、通知规则 |

发布保存：`{ data, submit, revision?, securityLevel, priority }`。data为完整应用资料；修改必须回传revision。密级public/internal/confidential，优先级normal/high/urgent。草稿允许缺项，提交校验当前规则，保存submissionRules及formRevision。已提交资料锁定。

审批：`{ decision, reason, revision, scores? }`。approved必须给completeness/usability/security三项1—5分且都满足阈值；其他结论必须说明原因。过期修改返回409。审核快照、上架版本、来源、原有统计/范围与所有权保持一致。

范围：`{ scope: all|organizations|members|projects, targets, revision }`，实际检查在后端。演示对象可通过会话接口查看；正式IAM目录尚待接入，不用演示对象冒充。

体验：`{ requestId, input: {}, options: {}, simulateFailure? }`。输入符合已声明JSON Schema；不加载远程Schema引用。相同请求不会重复计量，更换内容需要新编号。运行记录带mode=simulation，真实AI尚未接入。

同步：`{ externalId, externalVersion, requestId, simulateFailure? }`。返回running/succeeded/failed/unchanged及publicationId、mapping、时间与说明。同外部版本不重复建申请，新版本审核通过前不覆盖现有目录。

所有场景状态只存在同一进程内存；热重载/重启重置。生产环境不注册模拟业务路由。表单配置限于已有字段，工具链开发限于模拟入口；未实现任意字段编辑器或智能体编排工具。
