# 场景应用源码

本模块贯通用户端发布、管理端审核、目录展示、开放范围、体验与个人活动。路由处理输入和身份校验，业务规则通过服务、仓储和供应方适配器执行。

| 文件 | 职责 |
| --- | --- |
| `router.py`、`workflow_router.py`、`market_router.py` | 场景、发布审核、外部目录相关接口 |
| `application_service.py`、`workflows.py`、`market_workflows.py` | 业务规则与流程 |
| `ports.py`、`dependencies.py` | 仓储、身份及供应方边界与依赖组装 |
| `../common/resource_database.py` 及 `resource_*` | 新基线 PostgreSQL 资源、发布、版本、互动与文件读写 |
| `memory_repository.py`、`demo_store.py` | 独立内存演示 |
| `providers.py`、`market_contract.py`、`runtime.py` | 外部市场与智能体运行适配 |
| `demo_marketplace.py`、`sample_data.py` | 当前开发模拟所需资料 |
| `publication_schemas.py`、`workflow_schemas.py` | 发布和流程数据契约 |

配置 PostgreSQL 后固定使用 [新库基线](../../../database/resource-foundation/README.md)：`applications`、`application_publications`、`application_versions`、`application_dependencies` 及共用表。旧 `APPLICATIONS_STORAGE` 不再控制实际 PostgreSQL 装配；内存实现仅供隔离测试。平台发布资料不再保存到模拟快照目录，外部调用的临时状态使用 Redis。

主表只展示当前正式版本，待审修改不覆盖公开资料；审核、版本与文件归档同事务提交，通过审核不会自动解除禁用。资料下载、收藏、关注及评分使用共用明细，关联资源固化版本编号。

认证启用时使用公共会话提供的可信身份；关闭认证时才允许演示身份切换。管理员准入、开放范围、修订冲突及输入校验均由后端落实。

七项评分字段复用 `modules/common/review_schemas.py`；应用和数据集保留独立审核请求模型，各自决定哪些审核结论必须填写评分，不设置统一及格线。

教程和审核报告通过 `modules/common/file_transfer.py` 共用有界表单解析、上传名额及临时文件释放、Range/HEAD 和流式下载。路由先验证权限，并分别传入大小、表单字段、缓存策略；文件登记、业务归属及审核状态仍由各自服务处理。

供应方市场和智能体运行分别由 `APPLICATIONS_MARKET`、`APPLICATIONS_RUNTIME` 配置。模拟实现仍参与开发流程，真实服务接入按实际协议、账号和权限配置，凭据不进入前端或资料 JSON。

发布审核、存储、市场和身份集成的回归用例位于根目录 `tests/test_application_*.py`。接口细节可查看路由源码及开发环境 OpenAPI。
