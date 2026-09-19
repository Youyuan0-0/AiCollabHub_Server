"""系统事件的中文摘要映射；原始消息始终保留供核对。"""

from sqlalchemy import String, case, cast, literal
from sqlalchemy.sql.elements import ColumnElement

from app.modules.admin.logs.schemas import SOURCE_LABELS
from app.modules.common.system_logs import SystemLog

EVENT_SUMMARIES = {
    "auth_login_failed": "登录校验失败",
    "auth_csrf_failed": "请求安全校验未通过",
    "identity_sync_applied": "身份同步请求已处理",
    "identity_sync_duplicate": "重复的身份同步请求已忽略",
    "identity_sync_failed": "身份同步处理失败",
    "database_request_failed": "数据库请求执行失败",
    "database_probe_failed": "数据库健康检查失败",
    "commit_uncertain": "数据库事务提交结果待核对",
    "object_storage_unavailable": "文件存储服务暂时不可用",
    "storage_request_failed": "文件存储请求失败",
    "storage_probe_failed": "文件存储健康检查失败",
    "request_denied": "接口访问被拒绝",
    "request_failed": "接口请求处理失败",
    "request_unhandled_exception": "接口处理发生未捕获异常",
    "login_state_missing": "登录状态存储未配置",
    "login_state_save_failed": "登录状态保存失败",
    "login_state_consume_failed": "登录状态读取或校验失败",
    "cache_read_failed": "缓存读取失败",
    "cache_write_failed": "缓存写入失败",
    "cache_invalidation_failed": "缓存清理失败",
    "cache_probe_failed": "缓存健康检查失败",
    "cache_encoding_invalid": "缓存文本编码无效",
    "cache_json_invalid": "缓存数据格式无效",
    "overview_read_failed": "首页统计快照读取失败",
    "overview_write_failed": "首页统计快照保存失败",
    "overview_invalid": "首页统计快照格式无效",
    "overview_demo_invalid": "首页模拟数据格式无效",
    "feedback_cleanup_deferred": "反馈附件清理已延后",
    "application_snapshot_unavailable": "场景应用资源快照不可用",
}

# 旧采集点只提供默认事件编码，以受控消息前缀兼容历史记录，无需改写原文。
MESSAGE_EVENTS = {
    "Database readiness probe failed": "database_probe_failed",
    "Commit outcome requires reconciliation": "commit_uncertain",
    "Object storage request failed": "storage_request_failed",
    "Object storage probe failed": "storage_probe_failed",
    "Login state storage is not configured": "login_state_missing",
    "Login state save failed": "login_state_save_failed",
    "Login state consume failed": "login_state_consume_failed",
    "Cache read failed": "cache_read_failed",
    "Cache write failed": "cache_write_failed",
    "Cache invalidation failed": "cache_invalidation_failed",
    "Cache probe failed": "cache_probe_failed",
    "Invalid cached text encoding": "cache_encoding_invalid",
    "Invalid cached JSON": "cache_json_invalid",
    "Overview snapshot read failed": "overview_read_failed",
    "Overview snapshot write failed": "overview_write_failed",
    "Invalid overview snapshot": "overview_invalid",
    "Invalid overview demo data": "overview_demo_invalid",
    "Feedback cleanup deferred": "feedback_cleanup_deferred",
    "Application snapshot unavailable:": "application_snapshot_unavailable",
}

LEVEL_SUMMARIES = {"info": "运行信息", "warning": "运行告警", "error": "运行错误"}


def summary_expression() -> ColumnElement[str]:
    """展示与关键词检索复用同一中文表达式，分页和筛选仍由数据库执行。"""
    source = case(SOURCE_LABELS, value=SystemLog.source, else_="系统")
    fallback = (
        source
        + literal("：")
        + case(LEVEL_SUMMARIES, value=cast(SystemLog.level, String), else_="运行事件")
    )
    return case(
        *[(SystemLog.event == event, summary) for event, summary in EVENT_SUMMARIES.items()],
        *[
            (SystemLog.message.startswith(prefix, autoescape=True), EVENT_SUMMARIES[event])
            for prefix, event in MESSAGE_EVENTS.items()
        ],
        else_=fallback,
    )
