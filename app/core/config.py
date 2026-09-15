from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


# 配置优先级：初始化参数 > 环境变量 > .env 文件 > 字段默认值。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # 环境变量或 .env 中的空值不覆盖默认配置。
        extra="ignore",
        hide_input_in_errors=True,  # 校验失败时也不把整份含密码的输入写入启动日志。
    )

    app_name: str = "AiCollabHub API"
    app_env: Literal["development", "test", "production"] = "development"
    instance_id: str = Field(default="api", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    db_require_sync_apply: bool = False
    db_statement_timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    db_commit_timeout_ms: int = Field(default=10000, ge=1000, le=120000)
    # 推荐分字段填写；各连接独立配置，避免备库意外使用主库写账号。
    postgres_host: str | None = None
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_read_host: str | None = None
    postgres_read_port: int = Field(default=5432, ge=1, le=65535)
    postgres_read_db: str | None = None
    postgres_read_user: str | None = None
    postgres_read_password: SecretStr | None = None
    postgres_read_fallback_host: str | None = None
    postgres_read_fallback_port: int = Field(default=5432, ge=1, le=65535)
    postgres_read_fallback_db: str | None = None
    postgres_read_fallback_user: str | None = None
    postgres_read_fallback_password: SecretStr | None = None
    postgres_migration_host: str | None = None
    postgres_migration_port: int = Field(default=5432, ge=1, le=65535)
    postgres_migration_db: str | None = None
    postgres_migration_user: str | None = None
    postgres_migration_password: SecretStr | None = None
    # 连接池和迁移继续读取这些统一属性；显式 URL 优先，兼容既有配置和 SQLite 测试。
    # 连接串及分字段密码均使用 SecretStr 隐藏展示，并非加密存储。
    database_url: SecretStr | None = None
    database_read_url: SecretStr | None = None
    database_read_fallback_url: SecretStr | None = None
    database_migration_url: SecretStr | None = None
    db_read_enabled: bool = False
    db_read_weight: int = Field(default=100, ge=0, le=100)
    db_read_fallback_enabled: bool = False
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: float = Field(default=5, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=1)
    db_connect_timeout: int = Field(default=5, ge=1)

    redis_host: str | None = None
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_user: str | None = None
    redis_password: SecretStr | None = None
    redis_db: int = Field(default=0, ge=0)
    redis_ssl: bool = False
    redis_url: SecretStr | None = None
    redis_key_prefix: str = Field(default="aicollabhub", pattern=r"^[A-Za-z0-9:_-]{1,64}$")
    redis_default_ttl: int = Field(default=300, ge=1)
    redis_timeout: float = Field(default=1, gt=0)
    redis_max_connections: int = Field(default=20, ge=1)

    minio_endpoint: AnyHttpUrl | None = None
    minio_access_key: SecretStr | None = None
    minio_secret_key: SecretStr | None = None
    minio_bucket: str = Field(default="aicollabhub-files", min_length=3, max_length=63)
    minio_region: str = "us-east-1"
    minio_timeout: float = Field(default=5, gt=0)
    minio_max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    cors_origins: list[str] = []
    # 本轮仅开放模拟 IAM；真实 CAS 签名规范确认前，不提供生产认证开关。
    auth_mode: Literal["disabled", "mock"] = "disabled"
    auth_public_origin: AnyHttpUrl = "http://127.0.0.1:18880"
    auth_mock_iam_url: AnyHttpUrl = "http://127.0.0.1:18002"
    auth_mock_secret: SecretStr | None = None
    auth_cookie_secure: bool = False
    auth_session_ttl: int = Field(default=28800, ge=60, le=86400)
    auth_login_ttl: int = Field(default=300, ge=30, le=600)
    auth_audit_retention_days: int = Field(default=90, ge=1)
    auth_mock_admin_subjects: list[str] = ["mock-admin"]

    @model_validator(mode="after")
    def assemble_service_urls(self) -> "Settings":
        for prefix, target in (
            ("postgres", "database_url"),
            ("postgres_read", "database_read_url"),
            ("postgres_read_fallback", "database_read_fallback_url"),
            ("postgres_migration", "database_migration_url"),
        ):
            if getattr(self, target) is not None:
                continue
            parts = {
                name: getattr(self, f"{prefix}_{name}")
                for name in ("host", "db", "user", "password")
            }
            if all(value is None for value in parts.values()):
                continue
            missing = [f"{prefix}_{name}".upper() for name, value in parts.items() if value is None]
            if missing:
                raise ValueError(
                    "Incomplete PostgreSQL configuration; missing " + ", ".join(missing)
                )
            # URL.create 处理 @、:、/、% 等字符，.env 中填写原始密码即可。
            url = URL.create(
                "postgresql+psycopg",
                host=parts["host"],
                port=getattr(self, f"{prefix}_port"),
                database=parts["db"],
                username=parts["user"],
                password=parts["password"].get_secret_value(),
                query={"connect_timeout": str(self.db_connect_timeout)},
            )
            setattr(self, target, SecretStr(url.render_as_string(hide_password=False)))
        if self.redis_url is None:
            if self.redis_host is None:
                if self.redis_user is not None or self.redis_password is not None:
                    raise ValueError("REDIS_HOST is required when Redis credentials are configured")
            else:
                password = (
                    self.redis_password.get_secret_value()
                    if self.redis_password is not None
                    else None
                )
                # 仅密码认证需要保留空用户名，生成 redis://:password@host/db。
                username = (
                    self.redis_user
                    if self.redis_user is not None
                    else ("" if password is not None else None)
                )
                url = URL.create(
                    "rediss" if self.redis_ssl else "redis",
                    host=self.redis_host,
                    port=self.redis_port,
                    username=username,
                    password=password,
                    database=str(self.redis_db),
                )
                self.redis_url = SecretStr(url.render_as_string(hide_password=False))
        return self

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        for url in (self.auth_public_origin, self.auth_mock_iam_url):
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in (None, "/")
            ):
                raise ValueError("Authentication URLs must be origins without credentials")
        if self.auth_mode == "mock":
            if self.app_env == "production":
                raise ValueError("Mock IAM is prohibited in production")
            if not self.database_url:
                raise ValueError(
                    "Mock authentication requires shared PostgreSQL (POSTGRES_* or DATABASE_URL)"
                )
            if not self.auth_mock_secret or len(self.auth_mock_secret.get_secret_value()) < 32:
                raise ValueError("AUTH_MOCK_SECRET must contain at least 32 characters")
            if self.auth_public_origin.scheme == "https" and not self.auth_cookie_secure:
                raise ValueError("HTTPS authentication requires secure cookies")
        return self

    @field_validator(
        "database_url",
        "database_read_url",
        "database_read_fallback_url",
        "database_migration_url",
        "redis_url",
        "minio_endpoint",
        "minio_access_key",
        "minio_secret_key",
        "postgres_host",
        "postgres_db",
        "postgres_user",
        "postgres_password",
        "postgres_read_host",
        "postgres_read_db",
        "postgres_read_user",
        "postgres_read_password",
        "postgres_read_fallback_host",
        "postgres_read_fallback_db",
        "postgres_read_fallback_user",
        "postgres_read_fallback_password",
        "postgres_migration_host",
        "postgres_migration_db",
        "postgres_migration_user",
        "postgres_migration_password",
        "redis_host",
        "redis_user",
        "redis_password",
        mode="before",
    )
    @classmethod
    def normalize_optional_service_setting(cls, value: object) -> object:
        # 空白连接串或凭证统一视为未配置，避免创建无效客户端。
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_service_configuration(self) -> "Settings":
        # 开发阶段可暂不接数据库，生产环境缺少连接串则在启动前报错。
        if self.app_env == "production" and self.database_url is None:
            raise ValueError("POSTGRES_* or DATABASE_URL is required in production")
        if self.db_read_enabled and (self.database_url is None or self.database_read_url is None):
            raise ValueError(
                "DB_READ_ENABLED requires primary and standby connections "
                "(POSTGRES_* and POSTGRES_READ_*, or DATABASE_URL and DATABASE_READ_URL)"
            )
        if self.db_read_fallback_enabled and (
            not self.db_read_enabled or self.database_read_fallback_url is None
        ):
            raise ValueError(
                "DB_READ_FALLBACK_ENABLED requires DB_READ_ENABLED and a fallback connection "
                "(POSTGRES_READ_FALLBACK_* or DATABASE_READ_FALLBACK_URL)"
            )
        # 缺少部分凭证是配置错误，不能把原本启用的文件服务静默关闭。
        minio_parts = (self.minio_endpoint, self.minio_access_key, self.minio_secret_key)
        if any(part is not None for part in minio_parts) and not all(
            part is not None for part in minio_parts
        ):
            raise ValueError("MinIO requires endpoint, access key and secret key together")
        endpoint = self.minio_endpoint
        if endpoint is not None and (
            endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in (None, "/")
            or endpoint.query is not None
            or endpoint.fragment is not None
        ):
            raise ValueError("MINIO_ENDPOINT must contain only scheme, host and optional port")
        return self
