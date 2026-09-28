"""工具链配置的固定协议字段；连接信息与业务表单、临时接口回执分开。"""

from copy import deepcopy
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, JsonValue, SecretStr, field_validator, model_validator

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.toolchain.network import http_options
from app.modules.common.toolchain.parameters import CHUNK_PARAMETERS, parameter_references

MODULE_NAMES = ("applications", "datasets", "knowledge", "tools", "models")
CREDENTIAL_NAMES = tuple(
    f"{module}{suffix}"
    for module in MODULE_NAMES
    for suffix in ("AccessKeyId", "AccessKeySecret", "AppKey")
) + ("appKey", "marketAdminToken", "knowledgeStorageAccessKey", "knowledgeStorageSecretKey")


def credential_name(value: str) -> bool:
    """只接受已声明凭据及按平台身份隔离的市场登录密码。"""
    return value in CREDENTIAL_NAMES or (
        value.startswith("marketPassword:")
        and 0 < len(value.removeprefix("marketPassword:")) <= 200
        and not any(ord(char) < 32 for char in value)
    )


class NetworkConfiguration(StrictPayload):
    """现场可选代理及自签 CA，复用字段规则但不共用配置值。"""

    proxy_url: AnyHttpUrl | None = None
    ca_certificate: str = Field(default="", max_length=32000)

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """代理填写完整 URL，不在地址中夹带账号或附加参数。"""
        if value and (
            value.username
            or value.password
            or value.query
            or value.fragment
            or value.path not in (None, "/")
        ):
            raise ValueError("代理只填写协议、主机和端口")
        return value

    @field_validator("ca_certificate")
    @classmethod
    def validate_certificate(cls, value: str) -> str:
        """保存前校验 PEM 证书，阻止错误证书导致后端重启失败。"""
        if value:
            try:
                http_options(ca_certificate=value)
            except ValueError:
                raise ValueError("请填写有效的 PEM 格式 CA 证书") from None
            except OSError:
                raise ValueError("请填写有效的 PEM 格式 CA 证书") from None
        return value


class PopConfiguration(NetworkConfiguration):
    """各模块复用同一字段结构，各自保存独立的 POP 连接取值。"""

    mode: Literal["disabled", "mock", "live"] = "disabled"
    url: AnyHttpUrl | None = None
    workspace: str = Field(default="", max_length=200)
    region: str = Field(default="", max_length=100)
    timeout: float = Field(default=30, gt=0, le=120)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("url")
    @classmethod
    def check_origin(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """POP 使用固定接口路径，只接受不含账号、查询参数的服务根地址。"""
        if value and (
            value.username
            or value.password
            or value.query
            or value.fragment
            or value.path not in (None, "/")
        ):
            raise ValueError("POP 地址只填写协议、主机和端口")
        return value

    @field_validator("allowed_subjects")
    @classmethod
    def check_subjects(cls, values: list[str]) -> list[str]:
        """身份授权使用稳定的 subject，不以姓名或空字符串替代。"""
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("授权账号标识不能为空或超过 200 字符")
        return list(dict.fromkeys(value.strip() for value in values))


class ApplicationBinding(StrictPayload):
    """将本地应用版本绑定到供应方智能体版本。"""

    resource_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    agent_code: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    input_mode: Literal["text", "json"] = "text"


class ApplicationConnection(NetworkConfiguration):
    """应用运行接口独立使用 APP_KEY，工作台地址仅用于打开开发页面。"""

    connection: PopConfiguration = Field(default_factory=PopConfiguration)
    runtime: Literal["simulation", "agent_dev"] = "simulation"
    response_mode: Literal["json", "sse"] = "json"
    url: AnyHttpUrl | None = None
    timeout: float = Field(default=20, gt=0, le=120)
    entry_url: AnyHttpUrl | None = None
    bindings: list[ApplicationBinding] = Field(default_factory=list, max_length=1000)
    market: "MarketConnection" = Field(default_factory=lambda: MarketConnection())

    @field_validator("url", "entry_url")
    @classmethod
    def check_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """凭证由独立密钥字段保存，禁止在服务或工作台地址中夹带账号信息。"""
        if value and (value.username or value.password or value.query or value.fragment):
            raise ValueError("地址不能包含账号、密码、查询参数或片段")
        return value

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        """当前适配器按本地应用编号查找绑定，禁止重复覆盖。"""
        identifiers = [item.resource_id for item in self.bindings]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("本地应用编号不能重复绑定")
        return self


class DatasetConnection(StrictPayload):
    """数据集独立连接 Data Engine，开发加工仍在任务表指定的工作台完成。"""

    connection: PopConfiguration = Field(default_factory=PopConfiguration)
    entry_url: AnyHttpUrl | None = None

    _check_url = field_validator("entry_url")(ApplicationConnection.check_url.__func__)


class MarketAccount(StrictPayload):
    """平台用户对应供应方开发者账号，访问令牌由登录接口动态获取。"""

    subject: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=200)


class MarketResourceBinding(StrictPayload):
    """后台填写平台应用与 HiMarket 资源的对应关系。"""

    application_id: str = Field(min_length=1, max_length=200)
    owner_subject: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    listing_id: str | None = Field(default=None, min_length=1, max_length=200)
    portal_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketConnection(NetworkConfiguration):
    """已有 HiMarket 直连发布适配器的连接、账号、资源和状态映射。"""

    mode: Literal["disabled", "himarket"] = "disabled"
    url: AnyHttpUrl | None = None
    timeout: float = Field(default=15, gt=0, le=120)
    profile: str = Field(default="himarket-direct", min_length=1, max_length=80)
    accounts: list[MarketAccount] = Field(default_factory=list, max_length=500)
    bindings: list[MarketResourceBinding] = Field(default_factory=list, max_length=1000)
    decision_status: dict[Literal["approved", "rejected", "needs_changes"], str] = Field(
        default_factory=dict
    )
    # 旧配置中的环境凭据引用继续保留，界面维护账号密码后优先通过登录获取短期令牌。
    developer_token_env: dict[str, str] = Field(default_factory=dict)
    _check_url = field_validator("url")(ApplicationConnection.check_url.__func__)

    @model_validator(mode="after")
    def validate_mappings(self) -> Self:
        """复用已有市场校验，拒绝重复平台应用、资源、条目和账号。"""
        from app.modules.applications.market_contract import MarketConfiguration

        if len({item.subject for item in self.accounts}) != len(self.accounts):
            raise ValueError("同一平台账号不能重复配置市场登录")
        if len({item.application_id for item in self.bindings}) != len(self.bindings):
            raise ValueError("同一平台应用不能重复绑定市场资源")
        MarketConfiguration.model_validate(self.provider_configuration())
        references = [(item.resource_id, item.portal_id) for item in self.bindings]
        listings = [item.listing_id for item in self.bindings if item.listing_id]
        if len(set(references)) != len(references) or len(set(listings)) != len(listings):
            raise ValueError("外部资源或发布条目不能重复绑定")
        return self

    def provider_configuration(self) -> dict:
        """转换为已有发布流程接受的结构，不再维护第二套发布业务逻辑。"""
        return {
            "profile": self.profile,
            "developer_token_env": self.developer_token_env,
            "decision_status": self.decision_status,
            "bindings": {
                item.application_id: item.model_dump(exclude={"application_id"})
                for item in self.bindings
            },
        }


class KnowledgeStorageConfiguration(NetworkConfiguration):
    """知识库专用对象存储可独立配置，避免改动反馈和其他附件服务。"""

    mode: Literal["platform", "dedicated", "disabled"] = "platform"
    endpoint: AnyHttpUrl | None = None
    bucket: str = Field(default="knowledge-files", min_length=3, max_length=63)
    region: str = Field(default="us-east-1", min_length=1, max_length=100)
    timeout: float = Field(default=10, gt=0, le=120)
    _check_origin = field_validator("endpoint")(PopConfiguration.check_origin.__func__)

    @field_validator("bucket")
    @classmethod
    def check_bucket(cls, value: str) -> str:
        """保存前复用存储 SDK 的桶名规则，避免无效配置阻断启动。"""
        from minio.helpers import check_bucket_name

        check_bucket_name(value, strict=True)
        return value


class TenantBinding(StrictPayload):
    """可信平台组织与供应方 tenantId 的对应关系。"""

    organization_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)


class IndexMatch(StrictPayload):
    """保留任务要求的索引、模型及分段字段，不替用户删减条件。"""

    mode: Literal["高质量模式", "经济模式"]
    embedding_model: str = Field(min_length=1, max_length=200)
    chunk_mode: Literal["自动分段", "按段落", "固定长度"]
    chunk_size: int = Field(ge=100, le=2000)
    chunk_overlap: int = Field(ge=0, le=500)

    @model_validator(mode="after")
    def check_overlap(self) -> Self:
        """分段重叠必须小于分段大小，与知识库业务表单保持一致。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("分段重叠必须小于分段大小")
        return self


class IndexProfile(StrictPayload):
    """保存供应方确认的业务索引选项到 pipelineOptions 的对应关系。"""

    confirmed: bool = False
    match: IndexMatch
    dynamic_chunking: bool = False
    pipeline_options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        """动态分段必须传递两个数值，固定映射不允许未解析的占位符。"""
        references = parameter_references(self.pipeline_options)
        if self.dynamic_chunking and references != CHUNK_PARAMETERS:
            raise ValueError("跟随用户设置时，供应方参数须同时引用 ${chunkSize} 和 ${chunkOverlap}")
        if not self.dynamic_chunking and references:
            raise ValueError("使用分段占位符前，请启用跟随用户设置")
        return self


class RetrievalMatch(StrictPayload):
    """保留重排序、语义及关键词检索组合。"""

    reranking: bool
    semantic_search: bool
    keyword_search: bool

    @model_validator(mode="after")
    def check_search(self) -> Self:
        """与业务端一致，至少启用一种检索方式。"""
        if not self.semantic_search and not self.keyword_search:
            raise ValueError("至少启用语义检索或关键词检索")
        return self


class RetrievalProfile(StrictPayload):
    """保存供应方确认的检索选项映射。"""

    confirmed: bool = False
    match: RetrievalMatch
    retriever_specific_config: dict[str, JsonValue] = Field(default_factory=dict)


class KnowledgeConnection(StrictPayload):
    """仅支持当前 Knowledge Engine 接口族，不混用 Kortex 的编号和协议。"""

    connection: PopConfiguration = Field(default_factory=PopConfiguration)
    storage: KnowledgeStorageConfiguration = Field(default_factory=KnowledgeStorageConfiguration)
    ownership: Literal["personal", "organization"] = "personal"
    tenant_bindings: list[TenantBinding] = Field(default_factory=list, max_length=500)
    # 旧 JSON 通道只为已有现场兼容；新配置默认使用指南给出的 POP 表单绑定方式。
    body_encoding: Literal["form", "json"] = "form"
    retrieval_verified: bool = False
    index_profiles: list[IndexProfile] = Field(default_factory=list, max_length=100)
    retrieval_profiles: list[RetrievalProfile] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_profiles(self) -> Self:
        """拒绝同一条件的多份映射，避免实际调用取值不确定。"""
        for profiles in (self.index_profiles, self.retrieval_profiles):
            matches = [profile.match.model_dump_json() for profile in profiles]
            if len(matches) != len(set(matches)):
                raise ValueError("同一组选项不能重复配置映射")
        for index, profile in enumerate(self.index_profiles):
            for other in self.index_profiles[index + 1 :]:
                if (profile.dynamic_chunking or other.dynamic_chunking) and all(
                    getattr(profile.match, field) == getattr(other.match, field)
                    for field in ("mode", "embedding_model", "chunk_mode")
                ):
                    raise ValueError("跟随用户设置的映射不能与同模式、模型及分段方式的其他映射重叠")
        organizations = [item.organization_id for item in self.tenant_bindings]
        if len(organizations) != len(set(organizations)):
            raise ValueError("同一平台组织不能重复映射")
        return self


class ToolConnection(DatasetConnection):
    """工具成果复用 POP 连接和工作台地址，额外保存文档要求的查询范围。"""

    tool_category: Literal["my", "platform"] = "my"
    tool_types: list[Literal["api", "MY_MCP", "knowledge"]] = Field(
        default_factory=lambda: ["api", "MY_MCP", "knowledge"], min_length=1, max_length=3
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_mode(cls, value: object) -> object:
        """兼容此前仅保存开关的配置，读取不改写旧文件。"""
        if isinstance(value, dict) and "mode" in value and "connection" not in value:
            value = deepcopy(value)
            value["connection"] = {"mode": value.pop("mode")}
        return value


class ModelConnection(DatasetConnection):
    """AI Studio 模型仓库及版本使用独立 POP 连接。"""


class ToolchainConfiguration(StrictPayload):
    """按模块保存接入配置，保留各自支持的连接能力。"""

    applications: ApplicationConnection = Field(default_factory=ApplicationConnection)
    datasets: DatasetConnection = Field(default_factory=DatasetConnection)
    knowledge: KnowledgeConnection = Field(default_factory=KnowledgeConnection)
    tools: ToolConnection = Field(default_factory=ToolConnection)
    models: ModelConnection = Field(default_factory=ModelConnection)


class SaveToolchainConfiguration(StrictPayload):
    """空密钥表示保留；显式清除列表用于撤销已配置凭据。"""

    revision: str = Field(min_length=1, max_length=80)
    configuration: ToolchainConfiguration
    credentials: dict[str, SecretStr] = Field(default_factory=dict, max_length=520)
    clear_credentials: list[str] = Field(default_factory=list, max_length=520)

    @model_validator(mode="after")
    def check_credentials(self) -> Self:
        """拒绝未声明的凭据键，防止把任意配置写入密钥区域。"""
        if not all(credential_name(key) for key in (*self.credentials, *self.clear_credentials)):
            raise ValueError("凭据名称不属于当前接入配置")
        return self


class ConfigurationDocument(StrictPayload):
    """本地文件格式，密钥只在服务端保存，不通过读取接口回传。"""

    schema_version: Literal[2] = 2
    revision: str = Field(min_length=1, max_length=80)
    updated_at: str = ""
    updated_by: str = ""
    configuration: ToolchainConfiguration
    credentials: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_shared_connection(cls, value: object) -> object:
        """旧文件读取时拆分共享连接，保留原值；仅下次明确保存时写新版文件。"""
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion", value.get("schema_version")) != 1
        ):
            return value
        result = deepcopy(value)
        result.pop("schema_version", None)
        result["schemaVersion"] = 2
        config = result.get("configuration")
        if not isinstance(config, dict) or not isinstance(result.get("credentials", {}), dict):
            raise ValueError("旧配置文件格式不正确")
        common = config.pop("common", {})
        credentials = result.get("credentials", {})
        # v1 共享配置不包含 AI 工具，不能在升级时自动启用新增模块。
        for module in ("applications", "datasets", "knowledge"):
            if module in config and not isinstance(config[module], dict):
                raise ValueError("旧配置模块格式不正确")
            config.setdefault(module, {})["connection"] = deepcopy(common)
            for old, suffix in (
                ("accessKeyId", "AccessKeyId"),
                ("accessKeySecret", "AccessKeySecret"),
            ):
                if old in credentials:
                    credentials[module + suffix] = credentials[old]
        credentials.pop("accessKeyId", None)
        credentials.pop("accessKeySecret", None)
        config["knowledge"].setdefault("bodyEncoding", "json")
        return result

    def validate_connection(self) -> None:
        """启用真实接口时验证必要信息，不通过试调用创建外部资源。"""
        applications = self.configuration.applications
        for module, label in (
            ("applications", "场景应用"),
            ("datasets", "数据集"),
            ("knowledge", "知识库"),
            ("tools", "AI 工具"),
            ("models", "模型库"),
        ):
            connection = getattr(self.configuration, module).connection
            location = connection.workspace if module != "models" else connection.region
            if connection.mode == "live" and not all(
                (
                    connection.url,
                    location,
                    connection.allowed_subjects,
                    self.credentials.get(module + "AccessKeyId"),
                    self.credentials.get(module + "AccessKeySecret"),
                )
            ):
                location_label = "地域" if module == "models" else "工作空间"
                raise ValueError(
                    label + "的真实接口需要地址、" + location_label + "、AK/SK 和授权账号标识"
                )
        if applications.runtime == "agent_dev" and not all(
            (
                applications.url or applications.connection.url,
                self.credentials.get("appKey"),
            )
        ):
            raise ValueError("真实应用运行需要服务地址和 APP_KEY")
        storage = self.configuration.knowledge.storage
        if storage.mode == "dedicated" and not all(
            (
                storage.endpoint,
                self.credentials.get("knowledgeStorageAccessKey"),
                self.credentials.get("knowledgeStorageSecretKey"),
            )
        ):
            raise ValueError("知识库专用存储需要地址、AccessKey 和 SecretKey")
        if (
            self.configuration.knowledge.ownership == "organization"
            and not self.configuration.knowledge.tenant_bindings
        ):
            raise ValueError("组织知识库需要填写平台组织与 tenantId 的对应关系")
        market = applications.market
        if market.mode == "himarket" and not market.url:
            raise ValueError("外部市场需要填写服务地址")
        for account in market.accounts:
            if not self.credentials.get("marketPassword:" + account.subject):
                raise ValueError("市场开发者账号需要填写对应密码")
        subjects = {account.subject for account in market.accounts}
        if any(
            key.startswith("marketPassword:")
            and key.removeprefix("marketPassword:") not in subjects
            for key in self.credentials
        ):
            raise ValueError("市场密码必须对应已配置的开发者账号")
        if not all(credential_name(key) for key in self.credentials):
            raise ValueError("凭据名称不属于当前接入配置")
        for value in self.credentials.values():
            secret = value.get_secret_value()
            if not secret.strip() or len(secret) > 8192 or any(ord(char) < 32 for char in secret):
                raise ValueError("密钥不能为空、包含控制字符或超过 8192 字符")
