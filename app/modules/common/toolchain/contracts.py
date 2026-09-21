"""工具链来源、权限和分页共用规则，保留各产品的字段含义。"""

import hashlib
import json
from urllib.parse import quote

from fastapi import HTTPException

from app.core.config import Settings
from app.modules.common.identity import Actor


def fingerprint(value: object) -> str:
    """计算稳定的内容指纹，用于来源关联和重复导入判定。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def segment(value: str) -> str:
    """编码外部资源编号，拒绝路径跳转及控制字符。"""
    if not value or value in (".", "..") or any(ord(char) < 32 for char in value):
        raise HTTPException(422, "工具链资源编号无效")
    return quote(value, safe="")


def authorize(settings: Settings, actor: Actor) -> None:
    """仅让授权发布者访问配置的外部工作空间，真实模式不默认共享服务账号权限。"""
    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前账号没有工具链操作权限")
    if settings.toolchain_mode == "disabled":
        raise HTTPException(503, "工具链尚未配置")
    if settings.toolchain_mode == "live" and actor.id not in settings.toolchain_allowed_subjects:
        raise HTTPException(403, "当前账号未获授权访问此工具链工作空间")


def source_reference(settings: Settings, provider: str, identifier: str) -> dict:
    """来源键包含环境和工作空间，避免不同环境的相同编号发生碰撞。"""
    return {
        "provider": provider,
        "mode": settings.toolchain_mode,
        "environment": str(settings.toolchain_pop_url or "https://toolchain.invalid"),
        "workspace": settings.toolchain_workspace or "mock-workspace",
        "externalId": identifier,
    }


def object_result(value: object) -> dict:
    """检查供应方对象响应，格式变化时明确失败。"""
    if not isinstance(value, dict):
        raise HTTPException(502, "工具链接口返回的数据格式不正确")
    return value


def list_result(value: object) -> list[dict]:
    """检查供应方列表，拒绝将错误响应解释为空目录。"""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HTTPException(502, "工具链接口返回的列表格式不正确")
    return value
