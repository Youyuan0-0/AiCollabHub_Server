# 首页与运营看板基础接口

本阶段对齐 `D:\work\AiCollabHub_User` 的 `src/types/overview.ts` 和
`src/services/httpPlatformApi.ts`，保留 camelCase 响应字段，无额外 `data` 包装层。

## 实现范围与数据口径

- 首页：智能体数量、高质量数据集数量、模型数量、Token 摘要、算力摘要、热门资源推荐、
  最新动态、典型应用场景、问题建议、七类首页榜单。
- 运营详情：Token 汇总/趋势/维度分布、月度预算及预警、节点负载、资源池及作业队列、
  用户活跃榜/应用热度榜/组织贡献榜。
- 数据保存于 `overview_snapshots`，重启后保留；写入接口做类型、枚举、数量、百分比、
  站内链接及榜单分类唯一性校验。每个 PUT 是指定快照的完整替换，数组顺序即展示顺序。
- 非生产环境未发布的板块返回 `app/data/overview_demo.json` 中的固定样例，内容迁移自
  原前端 `src/mocks/home.ts` 与 `src/mocks/operations.ts`。已发布快照始终优先，
  主动清空的数组不会被样例重新填充；生产缺失时仍返回空态，不使用样例。
- 当前没有正式资源、计量、监控、评价、用户贡献业务表。数量、榜单、推荐和监控值由
  可信统计方或运营方发布；尚未实现自动计数、实时采集、按评分排序和按时间自动滚榜。
  首页摘要与详情分别发布，发布方应保证两者口径一致。
- 当前是平台共享快照，非生产环境可匿名读取，没有个人/组织数据权限裁剪。
  `人员` 是统计维度，不表示当前登录人。禁止把某个用户私有用量或未获准共享的组织数据
  发布到这些快照中。接入真实个人用量前，需要扩展服务层和存储键的用户/组织范围。
- 预算金额单位为元，非零金额至少 0.01 元；预算使用率和提示由服务端计算。
  预算大于零时超支允许超过 100%；零预算时使用率返回 0，通过提示说明无法计算。
- 前端现有趋势图接收 0～100 的归一化百分数，不能将 `trend` 当作原始 Token 明细。
  最新动态的 `time` 兼容展示字符串，建议发布绝对时间，避免“刚刚”随快照陈旧而失真。

## 数据库与身份

先配置已安装相应驱动的 `DATABASE_URL`，然后执行：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

迁移只建表，不导入演示内容，应用启动不自动建表或迁移。SQLite 仅用于本地验证，
生产数据库和对应驱动仍需另行确认。回退迁移会删除全部看板快照。

暂不实现登录，后续通过 `create_app(identity_provider=...)` 对接第三方单点登录：

- `APP_ENV=development` 或 `test`：仅 GET `/overview/home`、`/overview/operations`
  允许匿名读取，方便当前主页联调；不生成用户或会话。默认环境为 development。
- `APP_ENV=production`：上述 GET 仍要求可信身份及 `overview:read`。
- 所有环境的 PUT：始终要求可信身份及 `overview:read`、`overview:write`。
- 其他接口继续遵守原有鉴权。未配置身份时 `/auth/session` 返回 401，
  `/auth/login-url` 返回 503。前端混合模式的资源模块及会话使用原有 Mock，
  所有受保护页面仍初始化会话并检查权限；全 HTTP 模式和生产使用后端真实会话。
- 前端演示身份仅服务于本地收藏及演示操作，不发送为可信身份，也不能调用看板发布接口。
- 只读策略取自服务端环境配置，客户端参数或 `X-Role` 等请求头不能修改。

本地联调建议使用独立数据库，避免已有 `development.db` 的不同迁移链冲突：

```powershell
$env:APP_ENV='development'
$env:DATABASE_URL='sqlite:///./.runtime/overview-integration.db'
New-Item -ItemType Directory -Path .runtime -Force | Out-Null
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

也可在未配置的项目 `.env` 中保存上述环境与连接串，系统环境变量优先于 `.env`。
迁移后可以匿名读取固定样例；发布快照仍需后续可信身份，暂不开放匿名写入。

## 端点

以下路径均以 `/api/v1` 为前缀。

| 方法与路径 | 用途 / 请求体 |
| --- | --- |
| GET `/overview/home` | 首页全部板块，兼容 `HomeOverview` |
| GET `/overview/operations` | 运营详情，兼容 `OperationsOverview` |
| PUT `/overview/home` | `HomeOverview` 完整数据，不含响应的 `metadata` |
| PUT `/overview/operations/token` | `tokenSummary`、`trend`、`tokenBreakdown` |
| PUT `/overview/operations/compute` | `nodeLoads`、`computeSummary`、`computePools` |
| PUT `/overview/operations/budget` | `used`、`limit`、可选 `warningThreshold`（默认 85） |
| PUT `/overview/operations/rankings` | `rankings`，每行固定为 `[名称, 指标]` |

运营 GET 支持以下参数；Token PUT 仅接收前两项，排行 PUT 仅接收后两项：

| 参数 | 可选值 | 默认 |
| --- | --- | --- |
| `tokenPeriod` | 日、周、月 | 月 |
| `tokenDimension` | 人员、组织、应用、模型 | 组织 |
| `rankingType` | 用户活跃榜、应用热度榜、组织贡献榜 | 用户活跃榜 |
| `rankingPeriod` | 日榜、周榜、月榜、总榜 | 周榜 |

例如 `GET /api/v1/overview/operations?tokenPeriod=周&tokenDimension=应用&rankingType=应用热度榜&rankingPeriod=月榜`。
每个已发布筛选组合独立保存，未录入组合在生产返回空数组，非生产返回固定样例。
样例沿用原前端行为：Token 总量按日/周/月切换，运营排行按三种类型切换；
Token 维度切换仍使用同一组组织分布，排行周期使用同一组样例，不代表真实筛选统计。
算力和月度预算不随上述筛选改变。相同键更新采用最后成功写入值；并发首次创建冲突返回 409，
调用方可重试完整 PUT，目前没有编辑版本冲突检测。

PUT 成功返回：

```json
{"section":"token","variant":"月:组织","updatedAt":"2026-09-14T02:00:00Z"}
```

GET 额外附带 `metadata`，前端据此展示各板块发布时间、缺失提示和预算空态：

```json
{
  "dataSource": "snapshot",
  "updatedAt": {"token": null, "compute": null, "budget": null, "rankings": null},
  "missingSections": ["token", "compute", "budget", "rankings"],
  "demoSections": []
}
```

首页元信息使用 `home` 板块键。生产首次查询时数量显示 `—`，其余首页板块为空；预算提示
“暂无预算数据”。有快照但数组为空表示发布方主动清空，不视为缺失。
更新时间为本次发布的 UTC 时间，不代表业务事件时间；快照不会自动过期。

非生产未发布时首页数量为 `126`、`92`、`48`，Token 月总量为 `45.2M`，包含完整推荐、
场景、动态、七组首页榜单和运营看板。`dataSource` 保留 `snapshot` 以兼容现有字段，
新增 `demoSections` 列出来自固定文件的板块；这些板块不列入 `missingSections`，
以便前端正常展示预算等内容，`updatedAt` 为 null，不伪造发布时间。
预算仍按统一公式计算，样例 `86420 / 120000` 对应 72.02%，其余展示文案沿用原前端。
数据库连接失败、迁移缺失、快照损坏均继续报错，不以固定样例掩盖服务异常。

开发服务启动后可直接验证，无需登录：

```powershell
curl.exe "http://127.0.0.1:8000/api/v1/overview/home"
curl.exe "http://127.0.0.1:8000/api/v1/overview/operations"
```

## 发布示例

以下为接口格式样例，数值仅用于联调，不会自动写入数据库。
`PUT /overview/home` 的完整请求体：

```json
{
  "homeStats": [
    {"label":"智能体数量","value":"12"},
    {"label":"高质量数据集","value":"8"},
    {"label":"模型数量","value":"3"}
  ],
  "tokenMetrics": [{"label":"组织维度","value":"1.2M","description":"本月平台组织汇总"}],
  "computeMetrics": [{"label":"GPU 利用率","value":42}],
  "featuredResources": [
    {"type":"大模型","name":"设计助手","description":"辅助方案设计","path":"/models"}
  ],
  "applicationScenarios": [
    {"name":"设计优化","description":"辅助方案评估","path":"/applications"}
  ],
  "recentUpdates": [
    {"time":"2026-09-14 10:00","title":"设计助手上线","description":"可申请使用"}
  ],
  "questionSuggestions": ["如何申请使用平台 AI 资源？"],
  "leaderboardGroups": [
    {"id":"datasets","label":"数据集","items":[]},
    {"id":"models","label":"模型","items":[
      {"name":"设计助手","tags":["模型","设计"],"metric":"120","secondary":"15","tone":"model"}
    ]},
    {"id":"applications","label":"应用","items":[]},
    {"id":"rated","label":"高分好评","items":[]},
    {"id":"popular","label":"人气热门","items":[]},
    {"id":"new","label":"新品首发","items":[]},
    {"id":"users","label":"用户榜","items":[]}
  ]
}
```

`PUT /overview/operations/token?tokenPeriod=月&tokenDimension=组织`：

```json
{
  "tokenSummary":[{"label":"Token总量","value":"1.2M","note":"本月平台汇总"}],
  "trend":[20,40,65,100],
  "tokenBreakdown":[{"name":"研发中心","amount":"0.6M","percent":50}]
}
```

`PUT /overview/operations/compute`：

```json
{
  "nodeLoads":["low","medium","high"],
  "computeSummary":[{"label":"GPU利用率","value":"42%","note":"采集时刻汇总","tone":"blue"}],
  "computePools":[{"name":"推理池","gpu":"42%","memory":"50%","jobs":2,"status":"正常"}]
}
```

`PUT /overview/operations/budget`：

```json
{"used":1200,"limit":10000,"warningThreshold":85}
```

`PUT /overview/operations/rankings?rankingType=应用热度榜&rankingPeriod=周榜`：

```json
{"rankings":[["设计助手","120 次"],["知识问答助手","80 次"]]}
```

## 错误与前端联调

- 422：参数不在枚举范围、结构不完整、百分比越界、非法入口路径等。
- 409 / `overview_write_conflict`：并发首次发布同一板块和筛选键。
- 503 / `database_not_configured`：未配置数据库。
- 503 / `overview_unavailable`：数据库不可用或迁移未执行。
- 503 / `overview_snapshot_invalid`：持久化内容不符合当前契约；不会降级为伪造成功。
- 503 / `overview_demo_invalid`：后端固定样例文件缺失或格式不符合契约。

前端已接入上述两个 GET，开发和生产环境均默认使用 HTTP；仅开发演示可显式配置
`VITE_API_MODE=mock`，修改配置后需重启开发服务。开发首页读取只需已配置并迁移的数据库；
生产读取及所有发布操作仍需可信身份与上述权限。前端开发混合配置为
`VITE_API_MODE=http`、`VITE_API_SCOPE=overview`：看板和开发问答历史走 HTTP，
资源目录等其他模块及会话保留原有演示实现。已有配置只指定 `http` 而未指定范围时，
继续全站 HTTP；尚未实现的业务接口会返回真实失败，不回退为模拟成功。

前端已处理板块发布时间、缺失预算隐藏金额、空数组空态、权限/服务错误提示和重试，
移除了“截至今日”文案及尚无后端支持的下钻按钮；历史对话仅在打开历史面板时请求。
当前完成的是接口契约与前端接入，真实 SSO 和浏览器端到端联调尚未完成。

开发环境完整字段校验及交互文档见 `/docs`。
