"""数据集复用公共模拟报告上传，不创建表或修改数据库结构。"""

from fastapi import Request

from app.modules.common.identity import Actor
from app.modules.common.memory_reports import report_router
from app.modules.datasets.admin_service import DatasetAdminService
from app.modules.datasets.workspace import dataset_workspace


def record(request: Request, identifier: str, actor: Actor) -> dict:
    """附件与数据集审核共用权限判断。"""
    return DatasetAdminService(dataset_workspace(request)).publication(identifier, actor)


router = report_router("dataset", record)
