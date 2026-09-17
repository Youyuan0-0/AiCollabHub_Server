from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.modules.applications.application_service import application_service
from app.modules.applications.discovery import analyze, related
from app.modules.applications.identity import current_actor
from app.modules.applications.identity_directory import publication_defaults
from app.modules.applications.precheck import ApplicationPrecheck, precheck
from app.modules.applications.publication_schemas import SaveApplicationPublication
from app.modules.common.catalog import query_resources
from app.modules.common.schemas import CatalogQuery

# 按“HTTP 方法 + 路径”接收请求；/api/v1 前缀由 app/api/router.py 添加。
# FastAPI 把 JSON 正文解析为参数对象，再把函数返回的字典或列表转成 JSON 响应。
router = APIRouter(tags=["applications (mock)"])


@router.get("/application-publication-defaults", response_model=None)
def publication_identity_defaults(request: Request) -> dict:
    """读取当前发布身份可用的单位和联系人默认值。"""
    return publication_defaults(request, current_actor(request))


@router.post("/application-precheck", response_model=None)
def check_application(payload: ApplicationPrecheck, request: Request) -> dict:
    """预检表单并返回字段错误和分类建议，不保存发布资料。"""
    current_actor(request)
    service = application_service(request)
    with service.repository.transaction(is_readonly=True) as session:
        return precheck(payload, session.get_configuration(), service.workflows.policy_in(session))


@router.get("/applications/{application_id}/discovery", response_model=None)
def application_discovery(application_id: str, request: Request) -> dict:
    """基于有权访问的应用生成分类依据、关联图和相关推荐。"""
    service, actor = application_service(request), current_actor(request)
    source = service.resource(application_id, actor)
    # 先过滤访问权限，再分析推荐候选，避免推荐泄露不可见资源。
    return {**analyze(source), "related": related(source, service.resources(actor))}


# 返回表单可选择的关联资源，供前端 ApplicationDependencies 组件展示。
@router.get("/application-dependencies", response_model=None)
def dependencies(request: Request) -> list[dict]:
    """读取发布表单可关联的模型、数据集、知识库和工具。"""
    return application_service(request).provider.dependencies()


# 接收 getApplicationFormConfiguration() 的 GET 请求，返回公开的必填规则。
@router.get("/application-form-configuration", response_model=None)
def form_configuration(request: Request) -> dict:
    """读取公开的发布表单规则。"""
    return application_service(request).configuration(public=True)


# 应用编号取自 URL；返回版本申请，前端再用申请编号打开发布表单。
@router.post("/applications/{application_id}/version-publications", response_model=None)
def create_version_publication(application_id: str, request: Request) -> dict:
    """为已上架应用创建或取得待编辑的版本申请。"""
    return application_service(request).create_version(application_id, current_actor(request))


# query 来自 URL 查询参数；读取应用目录后按条件筛选、排序和分页。
@router.get("/applications", response_model=None)
def resources(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """按开放范围筛选应用，再按目录条件排序和分页。"""
    service = application_service(request)
    warnings: list[str] = []
    items = service.resources(current_actor(request), warnings=warnings)
    policy = service.workflows.policy()
    if policy["recommendedIds"] or policy["revision"] > 1:
        for item in items:
            item["badge"] = "推荐" if item["id"] in policy["recommendedIds"] else ""
    return {**query_resources(items, query), "warnings": warnings}


# 按路径中的应用编号返回完整详情，交给前端 ApplicationDetailDrawer 显示。
@router.get("/applications/{application_id}", response_model=None)
def application_detail(application_id: str, request: Request) -> dict:
    """按应用编号读取可访问的上架详情。"""
    return application_service(request).resource(application_id, current_actor(request))


# 查询发布申请列表；草稿和待审申请不会因为这次查询而进入上架目录。
@router.get("/application-publications", response_model=None)
def publications(request: Request) -> list[dict]:
    """读取当前身份可编辑的发布申请列表。"""
    return application_service(request).publications(current_actor(request))


# 按申请编号查询，供前端回填已保存的表单和查看审核状态。
@router.get("/application-publications/{publication_id}", response_model=None)
def publication_detail(publication_id: str, request: Request) -> dict:
    """读取可编辑申请的表单资料与审核状态。"""
    return application_service(request).publication(publication_id, current_actor(request))


# 【场景应用交互 4】接收新建：对应前端 POST /api/v1/application-publications。
# payload 是 JSON 正文解析出的参数对象；保存后返回的记录由 FastAPI 转为 201 JSON 响应。
@router.post("/application-publications", status_code=201, response_model=None)
def create_publication(payload: SaveApplicationPublication, request: Request) -> dict:
    """补齐发布身份默认资料后创建草稿或提交审核。"""
    actor = current_actor(request)
    defaults = publication_defaults(request, actor)
    data = payload.data.model_copy(
        update={
            "owner": payload.data.owner or defaults["owner"],
            "contact": payload.data.contact or defaults["contact"],
        }
    )
    return application_service(request).save(payload.model_copy(update={"data": data}), actor=actor)


# 接收 PUT 修改：编号取自 URL，资料取自 JSON 正文；成功后返回 200 和最新记录。
@router.put("/application-publications/{publication_id}", response_model=None)
def update_publication(
    publication_id: str,
    payload: SaveApplicationPublication,
    request: Request,
) -> dict:
    """按申请编号和 revision 更新草稿或重新提交审核。"""
    return application_service(request).save(payload, publication_id, current_actor(request))


# 会话由 app.api.conversations 统一提供数据库历史与多轮接口，此模块负责应用目录和发布。
