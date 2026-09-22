# 数据集模块

提供目录、组合筛选、排序、分页、详情、字段和脱敏样例、版本历史、收藏、评分、发布及审核。身份、CSRF、失败提示沿用平台公共能力。

## 存储

配置 PostgreSQL 后使用 `datasets`、`dataset_publications`、`dataset_versions` 及共用互动、审核、文件、范围表。`dataset_records` 不再是业务依赖，内存实现只用于隔离测试。安装新库只执行 `alembic -n resource_foundation upgrade head`，不要混用旧默认链或 `alembic.datasets.ini`。

主表保存当前正式概览，字段、样例和其他专属资料随发布申请提交并冻结到正式版本。待审修改不覆盖公开版本，审核结论、七维质量评分、报告关联与正式资料在同一事务更新。修订冲突返回 409，不部分保存。

首次审核通过仍为禁用，管理员另行启用后才进入目录。API 继续使用 `listingStatus=listed/unlisted`，数据库为 `status=enabled/disabled`。禁用后用户端目录、详情、榜单、收藏和下载均遵守状态与权限限制，历史资料和互动仍保留。

开放范围为全部用户或指定成员，成员从 `users` 获取，规则和成员关系使用共用可见范围表。表单单位名称不赋予权限，保存须携带当前修订。

## 接口

| 接口 | 职责 |
| --- | --- |
| `GET /api/v1/datasets`、`/datasets/{id}` | 用户目录、完整详情、分类筛选和权限过滤 |
| `GET /api/v1/datasets/rankings` | 当前可见资源的下载或收藏榜单 |
| `GET /api/v1/dataset-activity` | 当前用户收藏编号 |
| `PUT /api/v1/datasets/{id}/favorite` | 设置收藏状态，重复请求不累加 |
| `GET/PUT /api/v1/datasets/{id}/rating` | 当前用户评分和资源平均分 |
| `POST /api/v1/datasets/{id}/sample-download` | 下载公开样例并登记下载明细 |
| `GET /api/v1/dataset-publication-defaults` | 可信身份默认值与实际存储方式 |
| `GET/POST /api/v1/dataset-publications` | 本人申请及新建草稿、提交 |
| `GET/PUT /api/v1/dataset-publications/{id}` | 查看申请、修改未冻结的资料 |
| `GET /api/v1/admin/datasets`、`/datasets/{id}` | 含禁用资源的管理目录和详情 |
| `PUT /api/v1/admin/datasets/{id}/listing`、`/visibility` | 启停与可见成员设置 |
| `GET /api/v1/admin/dataset-publications` 及详情 | 审核队列、筛选和申请资料 |
| `POST /api/v1/admin/dataset-publications/{id}/review` | 审核并保存不可变结论和快照 |
| `/api/v1/admin/dataset-publications/{id}/report-files` | 当前审核员暂存报告；历史报告通过快照读取 |
| `GET /api/v1/admin/dataset-audit` | 历次审核、提交资料及报告 |

发布必填名称、版本、简介、详细说明、单位、联系人、用途、形态、字段 Schema、语言、格式、分级，记录数或数据量至少一项，0 是合法记录数。草稿允许缺项，提交和审核前由服务端校验。

审核评分为七项 1–5 整数：格式规范性、安全规范性、标注规范性、结构完整性、内容真实性、内容一致性、内容干净性。用户评分与质量评分分别保存，未知综合质量分不由平均值冒充。驳回须填写原因，发布者修改重提后仍保留上轮资料与结论。

收藏、评分各用户各资源一条。统计从 `resource_favorites/resource_ratings/resource_downloads` 聚合，不写主表计数。下载生成后登记 `link_issued`，响应输出结束后记录 `completed`，不代表用户已经在磁盘保存文件；当前范围仍只是脱敏样例，不等于第三方完整数据下载。

## 外部边界

本地发布通道使用平台正式表；第三方完整文件、外部版本、血缘、加工和授权接口仍待实际接入。外部调用失败不得用本地样例冒充成功；不能仅改连接地址就宣称完成联调。

## 验证

普通契约、筛选和权限用例位于 `tests/test_dataset_*.py`。新 PostgreSQL 仓储回归使用显式 `RESOURCE_FOUNDATION_TEST_DATABASE_URL`，在随机 schema 中运行并整体回滚：

```powershell
python -m pytest tests/test_foundation_module_flows.py -k dataset -q
python -m pytest tests/test_foundation_resource_storage.py -q
```

回归覆盖发布、审核、显式启用、收藏、评分、下载、报告上传、历史读取和引用保护。旧直接操作 `dataset_records` 的验证脚本已移除，不能用于新库。
