"""任务表第 59、62、70、87 行与任务书 2.2.8 的工具字段口径。"""

REQUIRED_FIELDS = {
    "name": "工具名称",
    "description": "工具简介",
    "descriptionMarkdown": "详细说明",
    "toolType": "工具类型",
    "tags": "功能标签",
    "sourceType": "来源类型",
    "environment": "依赖环境",
    "deployment": "部署方式",
    "inputSchema": "输入参数 Schema",
    "outputSchema": "输出参数 Schema",
    "license": "License",
    "dataLevel": "数据分级",
}
OPTIONAL_FIELDS = {
    "team": "研发团队",
    "technicalStack": "技术栈",
    "endpoint": "API 接口地址",
    "usageExample": "调用示例代码",
}
SYSTEM_FIELDS = {
    "id": "工具 ID",
    "owner": "所属单位",
    "contact": "联系人",
    "createdAt": "创建时间",
    "updatedAt": "更新时间",
    "version": "版本号",
    "versions": "版本历史与变更记录",
    "usage": "使用次数",
    "favoriteCount": "收藏数",
    "rating": "用户评分",
    "runtime.today": "日调用量",
    "runtime.averageResponseMs": "平均响应时间",
    "runtime.successRate": "成功率",
    "runtime.errorRate": "错误率",
}


def field_rules() -> list[dict]:
    """返回只读字段规则；配置页不能改变任务约定的必填性或伪造系统统计。"""
    result = []
    for fields, requirement, source in (
        (REQUIRED_FIELDS, "必填", "任务表第59行"),
        (OPTIONAL_FIELDS, "选填", "任务表第59行"),
        ({"inputExample": "输入示例", "outputExample": "输出示例"}, "选填", "任务表第57行详情展示"),
        (SYSTEM_FIELDS, "系统生成", "任务表第59、62、70、87行；任务书2.2.8"),
    ):
        result.extend(
            {"key": key, "label": label, "requirement": requirement, "source": source}
            for key, label in fields.items()
        )
    return result
