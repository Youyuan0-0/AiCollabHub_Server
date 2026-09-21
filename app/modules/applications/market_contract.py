"""HiMarket 直连接口：开发者 JWT 与管理面 Token 分开使用。

依据 AI Gateway 开发指南 20260831，§2.3.2、§2.3.4、§2.3.8、§2.3.12。
本模块不向浏览器返回原始响应里的凭证、端点或内部解析配置。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import Settings
from app.modules.applications.publication_schemas import ApplicationPayload, QualityScores
from app.modules.common.identity import Actor


class MarketBinding(BaseModel):
    """本地应用与外部资源、归属账号及发布条目的绑定关系。"""

    model_config = ConfigDict(extra="forbid")
    owner_subject: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    listing_id: str | None = Field(default=None, min_length=1, max_length=200)
    portal_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketConfiguration(BaseModel):
    """外部市场的运行配置、凭据引用和审核状态映射。"""

    model_config = ConfigDict(extra="forbid")
    profile: str = Field(min_length=1, max_length=80)
    developer_token_env: dict[str, str] = Field(default_factory=dict)
    bindings: dict[str, MarketBinding] = Field(default_factory=dict)
    # 指南仅以 PENDING 举例，决策枚举由供应方确认后配置，不能自行猜测。
    decision_status: dict[Literal["approved", "rejected", "needs_changes"], str] = Field(
        default_factory=dict
    )

    @field_validator("developer_token_env", "decision_status")
    @classmethod
    def nonempty_values(cls, value: dict[str, str]) -> dict[str, str]:
        """拒绝配置映射中的空键与空值。"""
        if any(not key.strip() or not item.strip() for key, item in value.items()):
            raise ValueError("Empty market configuration value")
        return value


class MarketDraft(ApplicationPayload):
    """仅允许 saveListing 文档列出的写入字段，不吞掉不支持的资料。"""

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=30000)
    default_version_id: str = Field(min_length=1, max_length=200)
    visibility: Literal["PRIVATE", "PUBLIC"] = "PRIVATE"
    category_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketMutation(ApplicationPayload):
    """外部发布操作的幂等标识、草稿内容和审核结论。"""

    request_id: str = Field(min_length=8, max_length=100)
    action: Literal["save_draft", "submit_review", "review_decision", "publish"]
    draft: MarketDraft | None = None
    submission_id: str | None = Field(default=None, min_length=1, max_length=200)
    decision: Literal["approved", "rejected", "needs_changes"] | None = None
    comment: str = Field(default="", max_length=2000)
    scores: QualityScores | None = None


class MarketRecovery(ApplicationPayload):
    """核对不确定操作时由用户补充的外部审核回执。"""

    external_submission_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketFault(HTTPException):
    """外部市场请求失败，附带结果是否不确定或已明确拒绝的信息。"""

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        status_code: int = 502,
        rejected: bool = False,
    ) -> None:
        super().__init__(status_code, message)
        self.uncertain = uncertain
        self.rejected = rejected


def select_fields(value: object, fields: Iterable[str]) -> dict:
    """校验外部对象并只保留允许向业务层传递的字段。"""
    if not isinstance(value, dict):
        raise MarketFault("外部服务返回的数据格式不正确")
    return {field: value[field] for field in fields if field in value}


def external_fields(value: object, fields: Iterable[str]) -> dict:
    """提取外部展示字段，并限制字段类型和文本长度。"""
    result = select_fields(value, fields)
    if any(item is not None and not isinstance(item, str) for item in result.values()):
        raise MarketFault("外部目录字段类型不正确")
    if any(len(item) > 30000 for item in result.values() if isinstance(item, str)):
        raise MarketFault("外部目录字段内容过长")
    return result


LISTING_FIELDS = (
    "listingId",
    "resourceId",
    "defaultVersionId",
    "title",
    "summary",
    "description",
    "visibility",
    "categoryId",
    "portalId",
    "publicationStatus",
    "updatedAt",
)
VERSION_FIELDS = ("versionId", "resourceId", "version", "status", "releaseNotes")
REVIEW_FIELDS = ("submissionId", "targetId", "status", "submittedAt", "decidedAt")


class HiMarketClient:
    """使用服务端凭据调用外部市场，校验回执并裁剪返回字段。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def configuration(self) -> MarketConfiguration:
        """加载并校验绑定配置，拒绝重复资源和不安全的服务地址。"""
        settings = self.settings
        if not (settings.applications_market_url and settings.applications_market_configuration):
            raise HTTPException(503, "外部目录尚未配置服务地址和应用对应关系")
        parsed = urlsplit(str(settings.applications_market_url))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(503, "外部目录地址配置不正确")
        try:
            config = MarketConfiguration.model_validate_json(
                Path(settings.applications_market_configuration).read_text(encoding="utf-8")
            )
            references = [(b.resource_id, b.portal_id) for b in config.bindings.values()]
            listings = [b.listing_id for b in config.bindings.values() if b.listing_id]
            if len(set(references)) != len(references) or len(set(listings)) != len(listings):
                raise ValueError("Duplicate binding")
            return config
        except (OSError, ValueError, ValidationError):
            raise HTTPException(503, "外部目录对应关系读取失败，请联系管理员") from None

    def headers(self, config: MarketConfiguration, *, subject: str | None = None) -> dict[str, str]:
        """按操作身份选择开发者或管理凭据，缺少凭据时拒绝请求。"""
        if subject is not None:
            env_name = config.developer_token_env.get(subject)
            token = os.environ.get(env_name, "") if env_name else ""
            if not token and env_name in self.settings.applications_market_developer_tokens:
                token = self.settings.applications_market_developer_tokens[
                    env_name
                ].get_secret_value()
            if not token or any(ord(char) < 32 for char in token):
                raise HTTPException(503, "当前账号尚未配置外部开发者登录凭证")
            return {"Authorization": "Bearer " + token}
        secret = self.settings.applications_market_admin_token
        if not secret or not secret.get_secret_value():
            raise HTTPException(503, "尚未配置外部目录管理凭证")
        return {
            "X-HIMARKET-OPENAPI": "HIMARKET_OPENAPI",
            "X-HIMARKET-TOKEN": secret.get_secret_value(),
        }

    def request(
        self,
        config: MarketConfiguration,
        method: str,
        path: str,
        *,
        subject: str | None = None,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        """调用外部接口并校验响应大小和业务状态，区分拒绝与结果不确定。"""
        headers = self.headers(config, subject=subject)
        is_mutation = method != "GET"
        try:
            with httpx.Client(
                base_url=str(self.settings.applications_market_url).rstrip("/"),
                headers=headers,
                timeout=self.settings.applications_market_timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    method, "/himarket/v1/open-api/v1" + path, params=params, json=body
                ) as response:
                    if response.status_code != 200:
                        raise MarketFault(
                            "外部目录请求失败，请检查权限或服务状态",
                            uncertain=is_mutation and response.status_code >= 500,
                            rejected=300 <= response.status_code < 500,
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise MarketFault("外部目录返回内容过大", uncertain=is_mutation)
                        chunks.append(chunk)
                    result = json.loads(b"".join(chunks))
        except httpx.TimeoutException:
            raise MarketFault(
                "等待外部目录超时，请先查询状态再操作", uncertain=is_mutation, status_code=504
            ) from None
        except (httpx.HTTPError, ValueError):
            raise MarketFault("外部目录连接失败或响应格式不正确", uncertain=is_mutation) from None
        if not isinstance(result, dict) or not isinstance(result.get("code"), str):
            raise MarketFault("外部目录响应缺少业务状态", uncertain=is_mutation)
        if result["code"] != "SUCCESS":
            raise MarketFault("外部目录未接受本次操作，请核对权限及资源状态", rejected=True)
        if not isinstance(result.get("data"), dict):
            raise MarketFault("外部目录响应缺少数据", uncertain=is_mutation)
        return result["data"]

    @staticmethod
    def page(value: dict, convert: Callable[[object], dict]) -> dict:
        """校验外部分页结构并逐条转换结果内容。"""
        content = value.get("content")
        if (
            not isinstance(content, list)
            or len(content) > 200
            or any(
                type(value.get(field)) is not int or value[field] < 0
                for field in ("pageNumber", "pageSize", "totalCount")
            )
        ):
            raise MarketFault("外部目录分页数据格式不正确")
        return {
            **select_fields(value, ("pageNumber", "pageSize", "totalCount")),
            "content": [convert(item) for item in content],
        }

    def catalog(
        self, config: MarketConfiguration, actor: Actor, keyword: str, page: int, size: int
    ) -> dict:
        """以开发者身份分页搜索外部应用目录。"""
        value = self.request(
            config,
            "GET",
            "/application/catalog/search",
            subject=actor.id,
            params={"keyword": keyword, "page": page, "size": size},
        )
        return self.page(
            value,
            lambda item: external_fields(
                item.get("catalog") if isinstance(item, dict) else None, LISTING_FIELDS
            ),
        )

    def listing(self, config: MarketConfiguration, listing_id: str) -> dict:
        """读取指定外部条目的公开资料字段。"""
        value = self.request(config, "GET", "/catalog/listings/" + quote(listing_id, safe=""))
        return external_fields(value, LISTING_FIELDS)

    def versions(
        self,
        config: MarketConfiguration,
        binding: MarketBinding,
        subject: str,
        page: int = 1,
        size: int = 100,
    ) -> dict:
        """读取绑定资源的版本列表并校验分页响应。"""
        value = self.request(
            config,
            "GET",
            "/me/resources/" + quote(binding.resource_id, safe="") + "/versions",
            subject=subject,
            params={"page": page, "size": size},
        )
        return self.page(value, lambda item: external_fields(item, VERSION_FIELDS))

    def version(
        self, config: MarketConfiguration, binding: MarketBinding, subject: str, version_id: str
    ) -> dict:
        """读取指定版本并确认它属于当前绑定资源。"""
        value = self.request(
            config,
            "GET",
            "/me/resources/"
            + quote(binding.resource_id, safe="")
            + "/versions/"
            + quote(version_id, safe=""),
            subject=subject,
        )
        version = external_fields(value.get("version"), VERSION_FIELDS)
        if (
            version.get("resourceId") != binding.resource_id
            or version.get("versionId") != version_id
        ):
            raise MarketFault("外部版本与当前资源不匹配")
        return version

    def review(self, config: MarketConfiguration, submission_id: str) -> dict:
        """读取审核回执并确认提交编号、目标条目与状态完整。"""
        value = self.request(config, "GET", "/catalog/reviews/" + quote(submission_id, safe=""))
        result = external_fields(value, REVIEW_FIELDS)
        if (
            result.get("submissionId") != submission_id
            or not result.get("status")
            or not result.get("targetId")
        ):
            raise MarketFault("外部审核回执缺少有效编号或状态")
        return result

    def categories(self, config: MarketConfiguration, page: int, size: int) -> dict:
        """分页读取外部市场分类并裁剪为允许展示的字段。"""
        value = self.request(
            config, "GET", "/catalog/categories", params={"page": page, "size": size}
        )
        return self.page(value, lambda item: external_fields(item, ("categoryId", "name", "kind")))
