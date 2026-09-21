"""按业务流程检查已保存接入配置；字段齐全不等于已经通过供应方联调。"""

from itertools import product

from app.core.config import Settings
from app.modules.common.toolchain.configuration_schemas import ConfigurationDocument


def configuration_readiness(document: ConfigurationDocument, baseline: Settings) -> dict:
    """列出三个模块的实际调用和缺项，只返回说明，不返回密钥或发起外部写入。"""
    config = document.configuration
    credentials = document.credentials

    def item(code: str, label: str, status: str, detail: str) -> dict:
        return {"code": code, "label": label, "status": status, "detail": detail}

    def connection(module: str, label: str, detail: str) -> dict:
        current = getattr(config, module).connection
        if current.mode != "live":
            return item(
                module + "-connection", label, "missing", "当前为模拟接口或未启用真实接口。"
            )
        missing = []
        for value, name in (
            (current.url, "服务地址"),
            (current.workspace, "工作空间"),
            (current.allowed_subjects, "授权账号"),
            (credentials.get(module + "AccessKeyId"), "AccessKey ID"),
            (credentials.get(module + "AccessKeySecret"), "AccessKey Secret"),
        ):
            if not value:
                missing.append(name)
        return item(
            module + "-connection",
            label,
            "missing" if missing else "configured",
            "缺少：" + "、".join(missing) if missing else detail,
        )

    def workbench(module: str) -> dict:
        return item(
            module + "-workbench",
            "进入开发工作台",
            "configured" if getattr(config, module).entry_url else "missing",
            "工作台地址用于打开开发页面；开发完成后返回平台拉取成果。"
            if getattr(config, module).entry_url
            else "请填写供应方交付的工作台页面地址。",
        )

    applications = [
        workbench("applications"),
        connection(
            "applications",
            "智能体目录、版本查询与成果导入",
            "共用该模块的管理接口地址及 AK/SK；导入后补齐任务表要求的发布资料。",
        ),
        item(
            "applications-runtime",
            "创建运行会话、调用应用与返回结果",
            "configured"
            if config.applications.runtime == "agent_dev"
            and (config.applications.url or config.applications.connection.url)
            and credentials.get("appKey")
            else "missing",
            "需启用真实运行并填写 APP_KEY；运行地址留空时沿用本模块管理接口地址。"
            "已上线导入应用自动使用来源编号，其他应用填写版本对应关系。",
        ),
        item(
            "applications-platform",
            "发布审核、可见范围、使用统计及评分",
            "platform",
            "沿用平台现有流程，任务书和任务表要求的业务字段由平台保存。",
        ),
    ]
    market = config.applications.market
    market_missing = []
    if market.mode == "himarket":
        if not market.url:
            market_missing.append("市场地址")
        if not market.accounts:
            market_missing.append("开发者账号")
        if not market.bindings:
            market_missing.append("应用资源对应关系")
        if not credentials.get("marketAdminToken"):
            market_missing.append("管理 Token")
        if any(
            not market.decision_status.get(key) for key in ("approved", "rejected", "needs_changes")
        ):
            market_missing.append("三种审核结果的供应方状态值")
        subjects = {
            account.subject
            for account in market.accounts
            if credentials.get("marketPassword:" + account.subject)
        }
        if any(binding.owner_subject not in subjects for binding in market.bindings):
            market_missing.append("资源维护人的开发者登录账号及密码")
    applications.append(
        item(
            "applications-market",
            "外部 HiMarket 发布与审核同步（可选）",
            "optional"
            if market.mode == "disabled"
            else "missing"
            if market_missing
            else "configured",
            "未启用；平台自身发布审核可以使用。"
            if market.mode == "disabled"
            else "缺少：" + "、".join(market_missing)
            if market_missing
            else "已配置直连模式的登录、目录、版本、发布、审核及状态核对；"
            "POP 网关协议不适用此连接。",
        )
    )
    datasets = [
        workbench("datasets"),
        connection(
            "datasets",
            "成果目录、详情、字段、预览与加工状态",
            "Data Engine 各接口路径由适配器按文档追加，无需逐个填写 URL。",
        ),
        connection(
            "datasets",
            "成果文件列表与临时下载链接",
            "与成果查询使用同一连接；按实际文件获取临时地址，并检查所属数据集。",
        ),
        item(
            "datasets-platform",
            "导入草稿、补充资料、审核及公开展示",
            "platform",
            "复用平台发布审核；供应方样例不自动公开，需提交脱敏样例。",
        ),
    ]
    datasets[2]["code"] = "datasets-files"
    knowledge = [
        connection(
            "knowledge",
            "建库、文档列表、处理状态、分段及文档删除",
            "Knowledge Engine 共用本模块的服务地址和鉴权配置。",
        )
    ]
    storage = config.knowledge.storage
    storage_ready = (
        storage.mode == "platform"
        and bool(
            baseline.minio_endpoint and baseline.minio_access_key and baseline.minio_secret_key
        )
    ) or (
        storage.mode == "dedicated"
        and bool(
            storage.endpoint
            and credentials.get("knowledgeStorageAccessKey")
            and credentials.get("knowledgeStorageSecretKey")
        )
    )
    knowledge.append(
        item(
            "knowledge-storage",
            "文档上传与交给引擎处理",
            "configured" if storage_ready else "missing",
            "需可用文件存储，并确认知识引擎能够读取临时文件地址。",
        )
    )
    expected = set(product(("高质量模式", "经济模式"), ("自动分段", "按段落", "固定长度")))
    supported = {
        (profile.match.mode, profile.match.chunk_mode)
        for profile in config.knowledge.index_profiles
        if profile.confirmed and profile.dynamic_chunking
    }
    missing_modes = [mode + " / " + chunk for mode, chunk in sorted(expected - supported)]
    knowledge.append(
        item(
            "knowledge-index",
            "索引模式、真实模型与完整分段数值范围",
            "missing" if missing_modes else "configured",
            "还缺支持用户分段数值的已确认映射：" + "、".join(missing_modes)
            if missing_modes
            else "六种索引/分段组合已覆盖；可选模型来自已确认映射。字符单位须与供应方核实。",
        )
    )
    expected_retrieval = {
        (rerank, semantic, keyword)
        for rerank, semantic, keyword in product((False, True), repeat=3)
        if semantic or keyword
    }
    supported_retrieval = {
        (profile.match.reranking, profile.match.semantic_search, profile.match.keyword_search)
        for profile in config.knowledge.retrieval_profiles
        if profile.confirmed
    }
    retrieval_missing = len(expected_retrieval - supported_retrieval)
    knowledge.append(
        item(
            "knowledge-retrieval-options",
            "重排序、语义及关键词检索组合",
            "missing" if retrieval_missing else "configured",
            f"六种有效检索组合中还有 {retrieval_missing} 种未配置确认。"
            if retrieval_missing
            else "六种组合均已配置；Top K 和相似度阈值由用户本次输入传递。",
        )
    )
    knowledge.append(
        item(
            "knowledge-retrieval-contract",
            "限定当前知识库并返回真实相似度",
            "configured" if config.knowledge.retrieval_verified else "supplier",
            "仍会逐次校验所属知识库、文档及分数，异常时明确报错。"
            if config.knowledge.retrieval_verified
            else "交付指南第 13 页记录过滤失效及缺少分数的问题，"
            "需供应方修复并在真实环境确认后启用。",
        )
    )
    knowledge.append(
        item(
            "knowledge-platform",
            "审核、可见范围、数量容量及召回统计",
            "platform",
            "平台复用审核权限，并按实际召回请求统计耗时和成功率；首次确认索引完成时记录时间。",
        )
    )
    return {
        module: {
            "configurationComplete": all(
                row["status"] not in ("missing", "supplier") for row in rows
            ),
            "checks": rows,
        }
        for module, rows in (
            ("applications", applications),
            ("datasets", datasets),
            ("knowledge", knowledge),
        )
    }
