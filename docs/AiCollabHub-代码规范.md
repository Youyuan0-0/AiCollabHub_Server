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
