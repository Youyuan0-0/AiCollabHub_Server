"""工具业务路由；模拟流程复用现有身份和 CSRF 检查。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.modules.common.identity import Actor, current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.interaction_schemas import FavoriteInput, RatingInput
from app.modules.common.toolchain.configuration import module_settings
from app.modules.common.toolchain.contracts import authorize
from app.modules.tools.schemas import InvokeTool, SaveTool, ToolCatalogQuery, ToolInput
from app.modules.tools.workspace import tool_workspace

router = APIRouter(tags=["tools (simulation)"])


def toolchain_actor(request: Request) -> Actor:
    """工具链成果独立校验启用状态和发布权限，不影响平台已有工具。"""
    actor = current_actor(request)
    authorize(module_settings(request.app.state.settings, "tools"), actor)
    return actor


@router.get("/tools", response_model=None)
def resources(
    request: Request,
    query: Annotated[ToolCatalogQuery, Query()],
) -> dict:
    """分页读取当前账号可见工具。"""
    return tool_workspace(request).catalog(
        query, current_actor(request), favorites_only=query.favorites_only
    )


@router.get("/tools/{tool_id}", response_model=None)
def detail(tool_id: str, request: Request) -> dict:
    """返回工具资料、当前账号互动和模拟统计。"""
    return tool_workspace(request).resource(tool_id, current_actor(request))


@router.put("/tools/{tool_id}/favorite", response_model=None)
def favorite(tool_id: str, payload: FavoriteInput, request: Request) -> dict:
    """幂等收藏或取消收藏。"""
    return tool_workspace(request).interact(
        tool_id, current_actor(request), favorite=payload.favorite
    )


@router.get("/tools/{tool_id}/definition", response_model=None)
def definition(tool_id: str, request: Request) -> dict:
    """返回智能体可引用的工具定义，不下发供应方内部地址或凭证。"""
    return tool_workspace(request).definition(tool_id, current_actor(request))


@router.put("/tools/{tool_id}/rating", response_model=None)
def rate(tool_id: str, payload: RatingInput, request: Request) -> dict:
    """沿用公共评分格式，每账号保留一份最新评分。"""
    return tool_workspace(request).interact(
        tool_id, current_actor(request), rating=payload.rating, comment=payload.comment
    )


@router.get("/tools/{tool_id}/ratings", response_model=None)
def ratings(tool_id: str, request: Request) -> list[dict]:
    """读取当前账号可访问工具的用户评价。"""
    return tool_workspace(request).ratings(tool_id, current_actor(request))


@router.post("/tools/{tool_id}/invoke", response_model=None)
def invoke(tool_id: str, payload: InvokeTool, request: Request) -> dict:
    """通过供应方适配器运行工具；当前仅提供模拟运行。"""
    return tool_workspace(request).invoke(tool_id, payload, current_actor(request))


@router.get("/tool-publication-defaults", response_model=None)
def defaults(request: Request) -> dict:
    """返回可信身份、表单字典及独立的工具链接入状态。"""
    mode = module_settings(request.app.state.settings, "tools").toolchain_mode
    available = mode != "disabled"
    return {
        **publication_defaults(request, current_actor(request)),
        "configuration": tool_workspace(request).configuration(),
        "mode": tool_workspace(request).provider.mode,
        "toolchainMode": mode,
        "toolchainAvailable": available,
        "toolchainUrl": tool_workspace(request).provider.entry_url() if available else "",
    }


@router.get("/tool-publications", response_model=None)
def publications(request: Request) -> list[dict]:
    """读取当前用户发布申请。"""
    return tool_workspace(request).publications(current_actor(request))


@router.post("/tool-publication-precheck", response_model=None)
def precheck(payload: ToolInput, request: Request) -> dict:
    """发布时实时预检拦截词，不保存草稿；最终提交仍独立校验。"""
    current_actor(request)
    return tool_workspace(request).precheck(payload)


@router.post("/tools/{tool_id}/versions", response_model=None)
def new_version(tool_id: str, request: Request) -> dict:
    """本人已发布工具创建下一版草稿，保留工具编号和已有统计。"""
    return tool_workspace(request).new_version(tool_id, current_actor(request))


@router.get("/tool-publications/{record_id}", response_model=None)
def publication(record_id: str, request: Request) -> dict:
    """读取本人完整草稿和审核记录。"""
    return tool_workspace(request).publication(record_id, current_actor(request))


@router.post("/tool-publications", status_code=201, response_model=None)
def create(payload: SaveTool, request: Request) -> dict:
    """创建独立工具发布申请。"""
    actor = current_actor(request)
    return tool_workspace(request).save(payload, actor, publication_defaults(request, actor))


@router.put("/tool-publications/{record_id}", response_model=None)
def save(record_id: str, payload: SaveTool, request: Request) -> dict:
    """按修订更新草稿或退回资料。"""
    actor = current_actor(request)
    return tool_workspace(request).save(
        payload, actor, publication_defaults(request, actor), record_id
    )


@router.get("/toolchain-tools", response_model=None)
def toolchain(request: Request) -> dict:
    """读取当前启用的工具成果连接，真实模式失败不回退模拟数据。"""
    toolchain_actor(request)
    provider = tool_workspace(request).provider
    return {
        "mode": module_settings(request.app.state.settings, "tools").toolchain_mode,
        "entryUrl": provider.entry_url(),
        "items": provider.list_tools(),
    }


@router.post("/toolchain-tools/{external_id}/import", response_model=None)
def import_tool(
    external_id: str,
    request: Request,
    version: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
) -> dict:
    """将工具链资料导入本人草稿，不直接上架。"""
    actor = toolchain_actor(request)
    return tool_workspace(request).import_tool(
        external_id, actor, publication_defaults(request, actor), version=version
    )
