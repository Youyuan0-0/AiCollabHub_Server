"""本地工具链配置仓库；原子写入、跨进程冲突保护及启动装配不依赖数据库。"""

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.modules.common.toolchain.configuration_schemas import (
    CREDENTIAL_NAMES,
    MODULE_NAMES,
    ApplicationBinding,
    ApplicationConnection,
    ConfigurationDocument,
    DatasetConnection,
    HuaweiConnection,
    KnowledgeConnection,
    MarketConnection,
    MarketResourceBinding,
    ModelConnection,
    PopConfiguration,
    SaveToolchainConfiguration,
    ToolchainConfiguration,
    ToolConnection,
)
from app.modules.common.toolchain.readiness import configuration_readiness

MAX_CONFIGURATION_BYTES = 1024 * 1024
CREDENTIAL_FIELDS = {
    "appKey": "applications_agent_key",
    "marketAdminToken": "applications_market_admin_token",
}


def module_settings(settings: Settings, module: str, provider: str | None = None) -> Settings:
    """按模块和供应方取独立连接；旧单供应方环境不能自动启用另一家。"""
    if module not in MODULE_NAMES:
        raise ValueError("未知工具链模块")
    updates = settings.toolchain_module_connections.get(module)
    if updates is None and module == "tools":
        updates = {"toolchain_mode": settings.toolchain_tools_mode, "toolchain_provider": "aliyun"}
    if updates is None and module == "models":
        updates = {"toolchain_mode": settings.toolchain_models_mode}
    specific = settings.model_copy(update=updates) if updates is not None else settings
    if provider is None:
        return specific
    if provider not in ("aliyun", "huawei"):
        raise ValueError("未知工具链供应方")
    registered = settings.toolchain_provider_connections.get(module, {}).get(provider)
    if registered is not None:
        return specific.model_copy(update=registered)
    if specific.toolchain_provider == provider:
        return specific
    return specific.model_copy(
        update={
            "toolchain_provider": provider,
            "toolchain_mode": "disabled",
            "toolchain_pop_url": None,
            "toolchain_api_key": None,
            "toolchain_access_key_id": None,
            "toolchain_access_key_secret": None,
            "toolchain_app_key": None,
            "toolchain_entry_url": None,
            "toolchain_workspace": "",
            "toolchain_region": "",
            "toolchain_allowed_subjects": [],
        }
    )


def legacy_json(path: str | None, inline: dict | None) -> dict:
    """首次迁移时兼容既有绑定文件，读取失败不以空配置覆盖。"""
    if inline is not None:
        return inline
    if not path:
        return {}
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("配置应为 JSON 对象")
    return result


def module_entry_url(settings: Settings, module: str, provider: str) -> str:
    """工作台地址跟随所属连接，旧单供应方地址不复用于另一家。"""
    specific = module_settings(settings, module, provider)
    if specific.toolchain_entry_url:
        return str(specific.toolchain_entry_url)
    if module_settings(settings, module).toolchain_provider != provider:
        return ""
    field = (
        "toolchain_"
        + {
            "applications": "application",
            "datasets": "dataset",
            "models": "model",
            "tools": "tool",
        }.get(module, "knowledge")
        + "_entry_url"
    )
    return str(getattr(settings, field, None) or "")


def environment_market(settings: Settings, *, load_legacy: bool = True) -> MarketConnection:
    """读取旧市场配置，v1 文件未管理该部分，升级时继续保留现场连接。"""
    market = (
        legacy_json(settings.applications_market_configuration, settings.applications_market_data)
        if load_legacy
        else {}
    )
    return MarketConnection(
        mode=settings.applications_market,
        url=settings.applications_market_url,
        proxy_url=settings.applications_market_proxy_url,
        ca_certificate=settings.applications_market_ca_certificate,
        timeout=settings.applications_market_timeout,
        profile=market.get("profile", "himarket-direct"),
        decision_status=market.get("decision_status", {}),
        developer_token_env=market.get("developer_token_env", {}),
        bindings=[
            MarketResourceBinding(application_id=identifier, **binding)
            for identifier, binding in market.get("bindings", {}).items()
        ],
        accounts=[
            {"subject": subject, "username": account["username"]}
            for subject, account in settings.applications_market_accounts.items()
        ],
    )


def environment_document(settings: Settings, *, load_legacy: bool = True) -> ConfigurationDocument:
    """从已有环境及绑定文件构建初始配置，保留原有服务地址和密钥。"""
    bindings = (
        legacy_json(settings.applications_agent_bindings, settings.applications_agent_bindings_data)
        if load_legacy
        else {}
    )
    knowledge = (
        legacy_json(settings.toolchain_knowledge_configuration, settings.toolchain_knowledge_data)
        if load_legacy
        else {}
    )
    connections = {
        module: PopConfiguration(
            mode=(specific := module_settings(settings, module, "aliyun")).toolchain_mode,
            provider=specific.toolchain_provider,
            polling_enabled=specific.toolchain_polling_enabled,
            poll_interval=specific.toolchain_poll_interval,
            url=specific.toolchain_pop_url,
            workspace=specific.toolchain_workspace,
            region=specific.toolchain_region,
            timeout=specific.toolchain_timeout,
            allowed_subjects=specific.toolchain_allowed_subjects,
            proxy_url=specific.toolchain_proxy_url,
            ca_certificate=specific.toolchain_ca_certificate,
        )
        for module in MODULE_NAMES
    }
    credentials = {
        key: getattr(settings, field)
        for key, field in CREDENTIAL_FIELDS.items()
        if getattr(settings, field)
    }
    for module in MODULE_NAMES:
        specific = module_settings(settings, module, "aliyun")
        for suffix, field in (
            ("AccessKeyId", "toolchain_access_key_id"),
            ("AccessKeySecret", "toolchain_access_key_secret"),
            ("AppKey", "toolchain_app_key"),
            ("ApiKey", "toolchain_api_key"),
        ):
            if getattr(specific, field):
                credentials[module + suffix] = getattr(specific, field)
        huawei = module_settings(settings, module, "huawei")
        if module != "tools" and huawei.toolchain_api_key:
            credentials[module + "ApiKey"] = huawei.toolchain_api_key
    for subject, account in settings.applications_market_accounts.items():
        if account.get("password"):
            credentials["marketPassword:" + subject] = account["password"]
    return ConfigurationDocument(
        revision="environment",
        configuration=ToolchainConfiguration(
            huawei={
                module: HuaweiConnection(
                    connection=PopConfiguration(
                        provider="huawei",
                        mode=(huawei := module_settings(settings, module, "huawei")).toolchain_mode,
                        url=huawei.toolchain_pop_url,
                        timeout=huawei.toolchain_timeout,
                        allowed_subjects=huawei.toolchain_allowed_subjects,
                        proxy_url=huawei.toolchain_proxy_url,
                        ca_certificate=huawei.toolchain_ca_certificate,
                        polling_enabled=huawei.toolchain_polling_enabled,
                        poll_interval=huawei.toolchain_poll_interval,
                    ),
                    entry_url=module_entry_url(settings, module, "huawei") or None,
                )
                for module in ("applications", "datasets", "knowledge", "models")
            },
            tools=ToolConnection(
                connection=connections["tools"],
                entry_url=module_entry_url(settings, "tools", "aliyun") or None,
                tool_category=settings.toolchain_tools_category,
                tool_types=settings.toolchain_tools_types,
            ),
            models=ModelConnection(
                connection=connections["models"],
                entry_url=module_entry_url(settings, "models", "aliyun") or None,
            ),
            applications=ApplicationConnection(
                connection=connections["applications"],
                runtime=settings.applications_runtime,
                response_mode=settings.applications_agent_response_mode,
                proxy_url=settings.applications_agent_proxy_url,
                ca_certificate=settings.applications_agent_ca_certificate,
                url=settings.applications_agent_url,
                timeout=settings.applications_agent_timeout,
                entry_url=module_entry_url(settings, "applications", "aliyun") or None,
                bindings=[
                    ApplicationBinding(resource_id=identifier, **binding)
                    for identifier, binding in bindings.items()
                ],
                market=environment_market(settings, load_legacy=load_legacy),
            ),
            datasets=DatasetConnection(
                connection=connections["datasets"],
                entry_url=module_entry_url(settings, "datasets", "aliyun") or None,
            ),
            knowledge=KnowledgeConnection.model_validate(
                {
                    **knowledge,
                    "connection": connections["knowledge"],
                    "bodyEncoding": knowledge.get(
                        "bodyEncoding",
                        settings.toolchain_body_encoding
                        if knowledge or settings.toolchain_mode != "disabled"
                        else "form",
                    ),
                }
            ),
        ),
        credentials=credentials,
    )


@contextmanager
def configuration_lock(path: Path) -> Iterator[None]:
    """使用系统文件锁串行保存，共享文件的多个 worker 不会互相覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        # Windows 允许锁定 EOF 之外的字节；不能先写初始化字节，
        # 否则另一 worker 已加锁时，这次写入会把正常竞争误报为文件权限错误。
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HTTPException(409, "其他管理员正在保存配置，请稍后重新读取") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ToolchainConfigurationStore:
    """分别维护磁盘配置和当前进程的启动快照。

    管理员保存后只更新文件，不热替换正在服务的客户端。view 通过 revision 与
    activeRevision 标明是否需要重启；多 worker 部署须整体重启，才能使用同一份新配置。
    """

    def __init__(self, settings: Settings) -> None:
        """读取一份启动快照，文件不存在时沿用现有环境配置。"""
        self.path = settings.toolchain_configuration_file.resolve()
        self.baseline = settings
        self.loaded_from_file = self.path.exists()
        try:
            # 文件一旦启用便独立管理工具链，不再依赖旧绑定文件是否仍存在。
            self.active = (
                self.read()
                if self.loaded_from_file
                else environment_document(settings, load_legacy=False)
            )
        except (OSError, ValueError, HTTPException):
            raise RuntimeError("工具链配置读取失败，请检查本地配置文件，未自动覆盖原文件") from None

    def read(self) -> ConfigurationDocument:
        """读取已保存配置；损坏文件明确报错，不静默回退到默认值。"""
        try:
            if not self.path.exists() and not self.loaded_from_file:
                return environment_document(self.baseline)
            if self.path.stat().st_size > MAX_CONFIGURATION_BYTES:
                raise ValueError("配置过大")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            result = ConfigurationDocument.model_validate(raw)
            if isinstance(raw, dict) and raw.get("schemaVersion", raw.get("schema_version")) == 1:
                result.configuration.applications.market = environment_market(self.baseline)
                if self.baseline.applications_market_admin_token:
                    result.credentials["marketAdminToken"] = (
                        self.baseline.applications_market_admin_token
                    )
                for subject, account in self.baseline.applications_market_accounts.items():
                    if account.get("password"):
                        result.credentials["marketPassword:" + subject] = (
                            SecretStr(account["password"])
                            if isinstance(account["password"], str)
                            else account["password"]
                        )
            result.validate_connection()
            return result
        except (OSError, ValueError):
            raise HTTPException(503, "工具链配置文件无法读取，请检查文件格式和访问权限") from None

    def settings(self, baseline: Settings) -> Settings:
        """只覆盖白名单内的工具链接入参数，数据库及认证配置保持原有来源。"""
        if not self.loaded_from_file:
            return baseline
        config = self.active.configuration
        bindings = {
            binding.resource_id: binding.model_dump(exclude={"resource_id"})
            for binding in config.applications.bindings
        }
        updates = {
            "toolchain_tools_mode": config.tools.connection.mode,
            "toolchain_models_mode": config.models.connection.mode,
            "toolchain_tool_entry_url": config.tools.entry_url,
            "toolchain_model_entry_url": config.models.entry_url,
            "toolchain_tools_category": config.tools.tool_category,
            "toolchain_tools_types": config.tools.tool_types,
            "toolchain_module_connections": {},
            "toolchain_provider_connections": {},
            "toolchain_application_entry_url": config.applications.entry_url,
            "toolchain_dataset_entry_url": config.datasets.entry_url,
            "toolchain_knowledge_data": config.knowledge.model_dump(by_alias=True),
            "applications_runtime": config.applications.runtime,
            "applications_agent_url": config.applications.url or config.applications.connection.url,
            "applications_agent_timeout": config.applications.timeout,
            "applications_agent_response_mode": config.applications.response_mode,
            "applications_agent_proxy_url": config.applications.proxy_url,
            "applications_agent_ca_certificate": config.applications.ca_certificate,
            "applications_agent_bindings_data": bindings,
            "applications_agent_bindings": None,
            "toolchain_knowledge_configuration": None,
            "toolchain_knowledge_storage": {
                **config.knowledge.storage.model_dump(),
                "access_key": self.active.credentials.get("knowledgeStorageAccessKey"),
                "secret_key": self.active.credentials.get("knowledgeStorageSecretKey"),
            },
            "applications_market": config.applications.market.mode,
            "applications_market_url": config.applications.market.url,
            "applications_market_timeout": config.applications.market.timeout,
            "applications_market_proxy_url": config.applications.market.proxy_url,
            "applications_market_ca_certificate": config.applications.market.ca_certificate,
            "applications_market_data": config.applications.market.provider_configuration(),
            "applications_market_configuration": None,
            "applications_market_accounts": {
                account.subject: {
                    "username": account.username,
                    "password": self.active.credentials.get("marketPassword:" + account.subject),
                }
                for account in config.applications.market.accounts
            },
        }
        for module in MODULE_NAMES:
            connection = getattr(config, module).connection
            updates["toolchain_module_connections"][module] = {
                "toolchain_mode": connection.mode,
                "toolchain_provider": connection.provider,
                "toolchain_polling_enabled": connection.polling_enabled,
                "toolchain_poll_interval": connection.poll_interval,
                "toolchain_api_key": self.active.credentials.get(module + "ApiKey"),
                "toolchain_pop_url": connection.url,
                "toolchain_workspace": connection.workspace,
                "toolchain_region": connection.region,
                "toolchain_timeout": connection.timeout,
                "toolchain_allowed_subjects": connection.allowed_subjects,
                "toolchain_access_key_id": self.active.credentials.get(module + "AccessKeyId"),
                "toolchain_access_key_secret": self.active.credentials.get(
                    module + "AccessKeySecret"
                ),
                "toolchain_app_key": self.active.credentials.get(module + "AppKey"),
                "toolchain_proxy_url": connection.proxy_url,
                "toolchain_ca_certificate": connection.ca_certificate,
                "toolchain_body_encoding": config.knowledge.body_encoding
                if module == "knowledge"
                else "json",
            }
            updates["toolchain_provider_connections"][module] = {
                "aliyun": updates["toolchain_module_connections"][module],
            }
            if module in config.huawei:
                huawei = config.huawei[module]
                connection = huawei.connection
                updates["toolchain_provider_connections"][module]["huawei"] = {
                    "toolchain_provider": "huawei",
                    "toolchain_mode": connection.mode,
                    "toolchain_pop_url": connection.url,
                    "toolchain_api_key": self.active.credentials.get(module + "ApiKey"),
                    "toolchain_timeout": connection.timeout,
                    "toolchain_allowed_subjects": connection.allowed_subjects,
                    "toolchain_proxy_url": connection.proxy_url,
                    "toolchain_ca_certificate": connection.ca_certificate,
                    "toolchain_polling_enabled": connection.polling_enabled,
                    "toolchain_poll_interval": connection.poll_interval,
                    "toolchain_entry_url": huawei.entry_url,
                    "toolchain_workspace": "",
                    "toolchain_region": "",
                    "toolchain_access_key_id": None,
                    "toolchain_access_key_secret": None,
                    "toolchain_app_key": None,
                }
        updates.update(
            {field: self.active.credentials.get(key) for key, field in CREDENTIAL_FIELDS.items()}
        )
        return baseline.model_copy(update=updates)

    def view(self, document: ConfigurationDocument | None = None) -> dict:
        """只返回字段、凭据存在状态和是否待重启，不返回任何已保存密钥。"""
        document = document or self.read()
        return {
            "revision": document.revision,
            "activeRevision": self.active.revision,
            "restartRequired": document.revision != self.active.revision,
            "updatedAt": document.updated_at,
            "updatedBy": document.updated_by,
            "configuration": document.configuration.model_dump(mode="json", by_alias=True),
            "credentialStatus": {
                key: bool(document.credentials.get(key))
                for key in (*CREDENTIAL_NAMES, *document.credentials)
            },
            "activeModes": {
                module: getattr(self.active.configuration, module).connection.mode
                for module in MODULE_NAMES
            },
            "activeRuntime": self.active.configuration.applications.runtime,
            "huaweiModes": {
                module: connection.connection.mode
                for module, connection in self.active.configuration.huawei.items()
            },
            "platformStorageConfigured": self.baseline.minio_endpoint is not None,
            "readiness": configuration_readiness(document, self.baseline),
            "huaweiReadiness": configuration_readiness(document, self.baseline, "huawei"),
        }

    def save(self, payload: SaveToolchainConfiguration, subject: str) -> dict:
        """持有文件锁校验修订、合并凭据，再原子替换配置文件。

        未传入或传入空字符串的凭据沿用原值，清除必须显式列入 clear_credentials；
        同一密钥不能同时清除和替换。subject 来自已认证管理员，仅用于记录修改者。
        保存成功不改变 active 启动快照，返回视图会提示是否需要重启。
        """
        try:
            with configuration_lock(self.path):
                current = self.read()
                if current.revision != payload.revision:
                    raise HTTPException(409, "配置已被其他管理员修改，请重新读取后再保存")
                credentials = current.credentials.copy()
                subjects = {
                    account.subject
                    for account in payload.configuration.applications.market.accounts
                }
                credentials = {
                    key: value
                    for key, value in credentials.items()
                    if not key.startswith("marketPassword:")
                    or key.removeprefix("marketPassword:") in subjects
                }
                for key in payload.clear_credentials:
                    credentials.pop(key, None)
                for key, value in payload.credentials.items():
                    if value.get_secret_value():
                        if key in payload.clear_credentials:
                            raise HTTPException(422, "同一密钥不能同时替换和清除")
                        credentials[key] = value
                document = ConfigurationDocument(
                    revision=uuid4().hex,
                    updated_at=datetime.now(UTC).isoformat(),
                    updated_by=subject,
                    configuration=payload.configuration,
                    credentials=credentials,
                )
                try:
                    document.validate_connection()
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                self._write(document)
                return self.view(document)
        except OSError:
            raise HTTPException(503, "配置文件保存失败，请检查本地目录访问权限") from None

    def _write(self, document: ConfigurationDocument) -> None:
        """先写同目录临时文件并刷盘，再替换目标文件，避免读者读取到半份 JSON。"""
        data = document.model_dump(mode="json", by_alias=True, exclude={"credentials"})
        data["credentials"] = {
            key: secret.get_secret_value() for key, secret in document.credentials.items()
        }
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > MAX_CONFIGURATION_BYTES:
            raise HTTPException(422, "配置文件不能超过 1 MiB")
        # 临时文件与目标处于同一文件系统，os.replace 才能以原子替换方式切换完整版本。
        descriptor, name = tempfile.mkstemp(prefix=".toolchain-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def parse_save_payload(raw: bytes) -> SaveToolchainConfiguration:
    """校验错误只给出字段路径和类型，避免 FastAPI 默认错误回显密钥输入。"""
    try:
        return SaveToolchainConfiguration.model_validate_json(raw)
    except ValidationError as error:
        details = [
            {"loc": item["loc"], "type": item["type"], "msg": "配置项格式或取值不正确"}
            for item in error.errors(include_input=False, include_context=False)
        ]
        raise HTTPException(422, details) from None
