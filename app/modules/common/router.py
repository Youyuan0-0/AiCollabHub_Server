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
    resource = find_sample(resource_id)
    # 通用入口同样检查数据集权限，避免绕过专属详情接口读取受限资源名称。
    if resource["kind"] == "dataset":
        from app.modules.applications.identity import current_actor
        from app.modules.datasets.workspace import dataset_workspace

        resource = dataset_workspace(request).resource(resource_id, current_actor(request))
    return {
        "id": service.receipt()["id"],
        "durationMs": 0,
        "text": f"{resource['name']} 的模拟响应：{payload.prompt[:200]}",
    }


@router.post("/resources/{resource_id}/access-requests", status_code=201, response_model=None)
def access_request(resource_id: str, payload: AccessInput) -> dict:
    """验证资源编号并生成模拟访问申请回执。"""
    find_sample(resource_id)
    return service.receipt()


@router.post("/publications", status_code=201, response_model=None)
def publication(payload: PublicationInput) -> dict:
    """接收通用发布资料；场景应用须使用独立发布入口。"""
    if payload.kind == "application":
        raise HTTPException(422, "请通过场景应用发布页面提交完整资料")
    if payload.submit and (not payload.name.strip() or not payload.summary.strip()):
        raise HTTPException(422, "name_and_summary_required")
    return service.receipt(payload.submit)
