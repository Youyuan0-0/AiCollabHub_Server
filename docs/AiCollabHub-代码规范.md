# AiCollabHub 代码规范

适用于服务端 Python/FastAPI，以及用户端、管理端 Vue/TypeScript。默认约束新增和修改的代码，历史代码随实际改动逐步整理。

## 1. 命名

| 对象 | Python | TypeScript / Vue |
| --- | --- | --- |
| 变量、参数、函数 | 下划线：`application_id`、`save_application()` | 小驼峰：`applicationId`、`saveApplication()` |
| 类、类型、接口 | 大驼峰：`ApplicationService` | 大驼峰：`ApplicationInfo` |
| 常量 | 全大写下划线：`MAX_PAGE_SIZE` | 全大写下划线：`MAX_PAGE_SIZE` |
| 布尔变量 | `is_visible`、`has_permission` | `isVisible`、`hasPermission` |
| 普通代码文件 | `application_service.py` | `applicationService.ts` |
| Vue 组件 / 页面 | — | `ApplicationCard.vue` / `ApplicationsView.vue` |
| 组合式函数 | — | `useApplications.ts` |

- 名称表达业务含义，避免拼音和 `data1`、`obj2` 等模糊名称。
- 不使用 C++ 中的 `m_`、`sName`、`nCount` 等作用域或类型前缀。
- 集合使用复数，例如 `applications`；CSS 类使用短横线，例如 `application-card`。
- 前后端 JSON 字段使用小驼峰。已有接口字段、状态值和数据库列名不能因整理代码而随意更改。

### 数据库与接口命名

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 数据表 | 英文复数、小写下划线；复合名称的主体使用复数 | `users`、`auth_sessions`、`operation_logs`、`iam_sync_receipts` |
| 数据库字段、Python ORM 属性 | 英文小写下划线，不使用驼峰或带引号的大小写名称 | `display_name`、`user_sync_time`、`record_type` |
| ORM 模型类 | 英文单数、大驼峰 | `User`、`AuthSession`、`OperationLog`、`IamSyncReceipt` |
| 主键、关联编号 | 主键统一为 `id`；关联字段为实体单数加 `_id` | `id`、`user_id`，不用 `users_id` |
| 平台日期时间 | 使用 `_at` 后缀，类型为带时区的日期时间 | `created_at`、`updated_at`、`expires_at`、`revoked_at` |
| 布尔、摘要字段 | 布尔使用 `is_`/`has_`；摘要使用 `_hash` | `is_enabled`、`has_permission`、`token_hash`、`payload_hash` |
| 平台接口 JSON 字段 | 小驼峰，由接口模型显式映射 | `display_name` → `displayName`、`user_id` → `userId` |
| 枚举值 | 项目自有值使用英文小写，多个单词用下划线 | `active`、`admin`、`ticket_revocation` |

- 使用有业务含义的模块前缀，如 `auth_`、`iam_`；不增加 `t_`、`tbl_` 或项目名等无业务含义前缀，避免保留字表名如 `user`。
- 本平台公开 JSON 契约使用小驼峰，数据库与 Python 内部使用下划线，通过已有接口模型/别名机制映射；不能对第三方报文做不加区分的自动改名。
- IAM 等第三方接口保持其原有字段和枚举值，例如 `appId`、`syncTime`、`userlinkedrole`；接入层负责与内部字段对应，不修改甲方协议。
- 第三方原始时间戳与平台日期时间分开：本次已确认的 `sync_time`、`user_sync_time`、`role_sync_time` 使用 `bigint` 保存 IAM `syncTime` 原值。注明来源及秒/毫秒等单位；协议未明确时标注待确认，不用本地接收时间代替。
- 主键、唯一约束、检查约束、外键和索引遵循后端 `app/db/base.py` 的现有命名约定：`pk_<table>`、`uq_<table>_<column>`、`ck_<table>_<rule>`、`fk_<table>_<column>_<referred_table>`、`ix_<table>_<column>`；复合约束或部分索引使用能说明字段或用途的后缀。手写 DDL 显式命名时也遵循该约定。
- 新表及尚未落地的设计按以上规则命名。已落地的表、列和已有接口不因规范整理直接重命名；需要调整时，随对应任务明确迁移、兼容与回滚方案。

## 2. 格式

- Python 使用 **4 个空格**缩进；TypeScript、Vue、CSS、JSON 使用 **2 个空格**，不用 Tab。
- Python 格式以项目 Ruff 配置为准；TypeScript 默认单引号、省略可选的行尾分号。
- 一行表达一条主要语句，复杂条件和较长对象分行书写，避免多层三元表达式。
- 文件使用 UTF-8，保留末尾换行；编辑时避免产生无关的整文件格式变化。

## 3. 中文注释

- 新增或修改的公共类、业务函数、接口方法补充中文说明。
- 注释说明职责、参数含义和重要限制；复杂逻辑解释“为什么这样处理”。
- 简单函数写一句说明即可；参数、返回值或异常容易误解时再补充详细注释。
- 不逐行复述代码，不保留大段注释掉的废弃实现。

Python 使用 docstring：

```python
def calculate_page_offset(page: int, page_size: int) -> int:
    """计算分页偏移量；页码从 1 开始，参数须已通过正整数校验。"""
    return (page - 1) * page_size
```

TypeScript 使用 JSDoc：

```typescript
/** 计算分页偏移量；页码从 1 开始，参数须已通过正整数校验。 */
export function calculatePageOffset(page: number, pageSize: number): number {
  return (page - 1) * pageSize
}
```

有用的业务注释示例：

```typescript
// 只接收当前请求的结果，避免切换应用后旧响应覆盖新页面。
```

## 4. 基本编码要求

- **明确类型**：Python 公共函数标注参数与返回类型；TypeScript 避免使用 `any` 绕过检查。
- **职责清楚**：后端路由接收请求，服务处理业务；前端页面组织交互，公共逻辑按需提取。
- **复用已有代码**：接口请求、登录校验、数据库访问和公共组件优先使用项目现有实现。
- **处理失败状态**：不能吞掉异常或把失败显示成成功；前端请求要处理加载和错误，保存失败保留输入。
- **防止误操作**：提交中禁止重复点击；权限和数据校验必须在后端落实。
- **保持代码整洁**：删除本次改动产生的无用导入、变量、调试输出和废弃代码，不顺带修改无关功能。
- **改完进行检查**：后端运行相关 Ruff 检查与测试，前端运行类型检查或构建；按改动验证相关功能，如实说明未验证项。
