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


@router.get("/application-publication-defaults")
def publication_identity_defaults(request: Request):
    return publication_defaults(request, current_actor(request))


@router.post("/application-precheck")
def check_application(payload: ApplicationPrecheck, request: Request):
    current_actor(request)
    svc = application_service(request)
    with svc.repository.transaction(readonly=True) as session:
        return precheck(payload, session.get_configuration(), svc.workflows.policy_in(session))


@router.get("/applications/{application_id}/discovery")
def application_discovery(application_id: str, request: Request):
    svc, actor = application_service(request), current_actor(request)
    source = svc.resource(application_id, actor)
    # 先过滤访问权限，再分析推荐候选，避免推荐泄露不可见资源。
    return {**analyze(source), "related": related(source, svc.resources(actor))}


# 返回表单可选择的关联资源，供前端 ApplicationDependencies 组件展示。
@router.get("/application-dependencies")
def dependencies(request: Request):
    return application_service(request).provider.dependencies()


# 接收 getApplicationFormConfiguration() 的 GET 请求，返回公开的必填规则。
@router.get("/application-form-configuration")
def form_configuration(request: Request):
    return application_service(request).configuration(public=True)


# 应用编号取自 URL；返回版本申请，前端再用申请编号打开发布表单。
@router.post("/applications/{application_id}/version-publications")
def create_version_publication(application_id: str, request: Request):
    return application_service(request).create_version(application_id, current_actor(request))


# query 来自 URL 查询参数；读取应用目录后按条件筛选、排序和分页。
@router.get("/applications")
def resources(request: Request, query: Annotated[CatalogQuery, Query()]):
    svc = application_service(request)
    items = svc.resources(current_actor(request))
    policy = svc.workflows.policy()
    if policy["recommendedIds"] or policy["revision"] > 1:
        for item in items:
            item["badge"] = "推荐" if item["id"] in policy["recommendedIds"] else ""
    return query_resources(items, query)


# 按路径中的应用编号返回完整详情，交给前端 ApplicationDetailDrawer 显示。
@router.get("/applications/{application_id}")
def application_detail(application_id: str, request: Request):
    return application_service(request).resource(application_id, current_actor(request))


# 查询发布申请列表；草稿和待审申请不会因为这次查询而进入上架目录。
@router.get("/application-publications")
def publications(request: Request):
    return application_service(request).publications(current_actor(request))


# 按申请编号查询，供前端回填已保存的表单和查看审核状态。
@router.get("/application-publications/{publication_id}")
def publication_detail(publication_id: str, request: Request):
    return application_service(request).publication(publication_id, current_actor(request))


# 【场景应用交互 4】接收新建：对应前端 POST /api/v1/application-publications。
# payload 是 JSON 正文解析出的参数对象；保存后返回的记录由 FastAPI 转为 201 JSON 响应。
@router.post("/application-publications", status_code=201)
def create_publication(payload: SaveApplicationPublication, request: Request):
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
@router.put("/application-publications/{publication_id}")
def update_publication(
    publication_id: str,
    payload: SaveApplicationPublication,
    request: Request,
):
    return application_service(request).save(payload, publication_id, current_actor(request))


# 会话由 app.api.conversations 统一提供数据库历史与多轮接口，此模块负责应用目录和发布。
