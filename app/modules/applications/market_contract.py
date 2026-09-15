"""HiMarket 直连接口：开发者 JWT 与管理面 Token 分开使用。

依据 AI Gateway 开发指南 20260831，§2.3.2、§2.3.4、§2.3.8、§2.3.12。
本模块不向浏览器返回原始响应里的凭证、端点或内部解析配置。
"""

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.modules.applications.publication_schemas import ApplicationPayload, QualityScores


class MarketBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_subject: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    listing_id: str | None = Field(default=None, min_length=1, max_length=200)
    portal_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketConfiguration(BaseModel):
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
    def nonempty_values(cls, value):
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
    request_id: str = Field(min_length=8, max_length=100)
    action: Literal["save_draft", "submit_review", "review_decision", "publish"]
    draft: MarketDraft | None = None
    submission_id: str | None = Field(default=None, min_length=1, max_length=200)
    decision: Literal["approved", "rejected", "needs_changes"] | None = None
    comment: str = Field(default="", max_length=2000)
    scores: QualityScores | None = None


class MarketRecovery(ApplicationPayload):
    external_submission_id: str | None = Field(default=None, min_length=1, max_length=200)


class MarketFault(HTTPException):
    def __init__(self, message: str, *, uncertain=False, status_code=502, rejected=False):
        super().__init__(status_code, message)
        self.uncertain = uncertain
        self.rejected = rejected


def select_fields(value, fields):
    if not isinstance(value, dict):
        raise MarketFault("外部服务返回的数据格式不正确")
    return {field: value[field] for field in fields if field in value}


def external_fields(value, fields):
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
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def configuration(self):
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

    def headers(self, config, *, subject=None):
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

    def request(self, config, method, path, *, subject=None, params=None, body=None):
        headers = self.headers(config, subject=subject)
        mutation = method != "GET"
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
                            uncertain=mutation and response.status_code >= 500,
                            rejected=300 <= response.status_code < 500,
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise MarketFault("外部目录返回内容过大", uncertain=mutation)
                        chunks.append(chunk)
                    result = json.loads(b"".join(chunks))
        except httpx.TimeoutException:
            raise MarketFault(
                "等待外部目录超时，请先查询状态再操作", uncertain=mutation, status_code=504
            ) from None
        except (httpx.HTTPError, ValueError):
            raise MarketFault("外部目录连接失败或响应格式不正确", uncertain=mutation) from None
        if not isinstance(result, dict) or not isinstance(result.get("code"), str):
            raise MarketFault("外部目录响应缺少业务状态", uncertain=mutation)
        if result["code"] != "SUCCESS":
            raise MarketFault("外部目录未接受本次操作，请核对权限及资源状态", rejected=True)
        if not isinstance(result.get("data"), dict):
            raise MarketFault("外部目录响应缺少数据", uncertain=mutation)
        return result["data"]

    @staticmethod
    def page(value, convert):
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

    def catalog(self, config, actor, keyword, page, size):
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

    def listing(self, config, listing_id):
        value = self.request(config, "GET", "/catalog/listings/" + quote(listing_id, safe=""))
        return external_fields(value, LISTING_FIELDS)

    def versions(self, config, binding, subject, page=1, size=100):
        value = self.request(
            config,
            "GET",
            "/me/resources/" + quote(binding.resource_id, safe="") + "/versions",
            subject=subject,
            params={"page": page, "size": size},
        )
        return self.page(value, lambda item: external_fields(item, VERSION_FIELDS))

    def version(self, config, binding, subject, version_id):
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

    def review(self, config, submission_id):
        value = self.request(config, "GET", "/catalog/reviews/" + quote(submission_id, safe=""))
        result = external_fields(value, REVIEW_FIELDS)
        if (
            result.get("submissionId") != submission_id
            or not result.get("status")
            or not result.get("targetId")
        ):
            raise MarketFault("外部审核回执缺少有效编号或状态")
        return result

    def categories(self, config, page, size):
        value = self.request(
            config, "GET", "/catalog/categories", params={"page": page, "size": size}
        )
        return self.page(value, lambda item: external_fields(item, ("categoryId", "name", "kind")))
