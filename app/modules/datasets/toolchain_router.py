"""工具链查询与草稿导入；后台继续复用原数据集审核流程。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.datasets.toolchain import DatasetToolchain

router = APIRouter(prefix="/dataset-toolchain", tags=["dataset toolchain"])


class DatasetImport(StrictPayload):
    """导入已预览的来源快照，避免用户点击期间成果已改变。"""

    external_id: str = Field(min_length=1, max_length=200)
    snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")


def service(request: Request) -> DatasetToolchain:
    """使用当前仓储实例，数据库配置切换不改变工具链协议。"""
    return DatasetToolchain(
        request.app.state.toolchain_clients["datasets"], request.app.state.dataset_workspace
    )


@router.get("/resources")
def resources(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="pageSize")] = 20,
    q: Annotated[str, Query(max_length=200)] = "",
) -> dict:
    """分页读取当前授权工作空间的数据集。"""
    return service(request).catalog(current_actor(request), page, page_size, q)


@router.get("/resources/{identifier}")
def detail(identifier: str, request: Request) -> dict:
    """读取待导入元数据及待补齐字段。"""
    return service(request).detail(current_actor(request), identifier)


@router.get("/resources/{identifier}/preview")
def preview(
    identifier: str, request: Request, subset: str = "default", split: str = "train"
) -> dict:
    """读取少量样例，仅供授权发布者核对。"""
    return service(request).preview(current_actor(request), identifier, subset, split)


@router.get("/tasks/{task_id}")
def task(task_id: str, request: Request, run_id: Annotated[str, Query(alias="runId")]) -> dict:
    """按加工任务和运行编号查询成果状态。"""
    return service(request).task(current_actor(request), task_id, run_id)


@router.get("/resources/{identifier}/files")
def files(
    identifier: str,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="pageSize")] = 20,
) -> dict:
    """分页读取所属工具链的成果文件元数据。"""
    return service(request).files(current_actor(request), identifier, page, page_size)


@router.post("/imports")
def import_draft(payload: DatasetImport, request: Request) -> dict:
    """创建幂等草稿，由原有发布页面补齐后提交审核。"""
    actor = current_actor(request)
    return service(request).import_draft(
        actor,
        payload.external_id,
        payload.snapshot,
        publication_defaults(request, actor),
    )


@router.get("/resources/{identifier}/download")
def download(
    identifier: str,
    request: Request,
    response: Response,
    file_path: Annotated[str, Query(alias="filePath", min_length=1, max_length=4096)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    """返回授权文件的临时地址并禁止响应缓存。"""
    response.headers["Cache-Control"] = "no-store"
    return service(request).download(current_actor(request), identifier, file_path, page)
