"""跨资源兼容入口属于业务编排；通用规则仍由 common 层提供。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.modules.common import service
from app.modules.common.catalog import find_sample
from app.modules.common.schemas import AccessInput, InvocationInput, PublicationInput

router = APIRouter(tags=["common (mock)"])


@router.get("/framework", response_model=None)
def framework(request: Request) -> dict:
    """返回当前模拟实例和已挂载的业务模块。"""
    return {
        "mode": "mock",
        "instance": request.app.state.settings.instance_id,
        "modules": ["datasets", "models", "applications", "knowledge", "tools", "common", "admin"],
    }


# 看板由 app.api.overview 读取数据库快照，避免相同路径仍命中固定 Mock。


@router.post("/resources/{resource_id}/invoke", response_model=None)
def invoke(resource_id: str, payload: InvocationInput, request: Request) -> dict:
    """验证样例存在及数据集开放范围后生成模拟调用结果。"""
    if resource_id.startswith("tool-"):
        from app.modules.common.identity import current_actor
        from app.modules.tools.schemas import InvokeTool
        from app.modules.tools.workspace import tool_workspace

        return tool_workspace(request).invoke(
            resource_id, InvokeTool(inputs={"text": payload.prompt}), current_actor(request)
        )
    resource = find_sample(resource_id)
    # 通用入口同样检查数据集权限，避免绕过专属详情接口读取受限资源名称。
    if resource["kind"] == "dataset":
        from app.modules.common.identity import current_actor
        from app.modules.datasets.workspace import dataset_workspace

        resource = dataset_workspace(request).resource(resource_id, current_actor(request))
    return {
        "id": service.receipt()["id"],
        "durationMs": 0,
        "text": f"{resource['name']} 的模拟响应：{payload.prompt[:200]}",
    }


@router.post("/resources/{resource_id}/access-requests", status_code=201, response_model=None)
def access_request(resource_id: str, payload: AccessInput, request: Request) -> dict:
    """验证资源编号并生成模拟访问申请回执。"""
    if resource_id.startswith("tool-"):
        from app.modules.common.identity import current_actor
        from app.modules.tools.workspace import tool_workspace

        tool_workspace(request).resource(resource_id, current_actor(request))
    else:
        find_sample(resource_id)
    return service.receipt()


@router.post("/publications", status_code=201, response_model=None)
def publication(payload: PublicationInput) -> dict:
    """拒绝旧通用发布，要求调用各资源独立表单与字段契约。"""
    raise HTTPException(422, "请通过对应资源的独立发布页面提交完整资料")
