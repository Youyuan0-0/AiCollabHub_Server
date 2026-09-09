from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 配置优先级：初始化参数 > 环境变量 > .env 文件 > 字段默认值。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # 环境变量或 .env 中的空值不覆盖默认配置。
        extra="ignore",
    )

    app_name: str = "AiCollabHub API"
    app_env: Literal["development", "test", "production"] = "development"
    # 连接串可能包含密码；SecretStr 默认隐藏输出中的敏感值，并非加密存储。
    database_url: SecretStr | None = None
    cors_origins: list[str] = []

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        # 直接传入的空白连接串也统一视为未配置，避免创建无效引擎。
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def require_production_database(self) -> "Settings":
        # 开发阶段可暂不接数据库，生产环境缺少连接串则在启动前报错。
        if self.app_env == "production" and self.database_url is None:
            raise ValueError("DATABASE_URL is required in production")
        return self
