"""按已确认的测试库基线一次性装配平台业务仓储。"""

from fastapi import FastAPI

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.db.foundation import FoundationDatabase
from app.modules.applications.demo_store import create_database_application_service
from app.modules.common.database_reports import DatabaseReports
from app.modules.common.database_workspace import DatabaseResourceWorkspace
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_runtime import ResourceRuntime
from app.modules.datasets.publishing import create_dataset_publisher
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace
from app.modules.knowledge.database import KnowledgeDatabase
from app.modules.knowledge.database_files import KnowledgeDatabaseFiles
from app.modules.knowledge.retrieval import RetrievalProvider
from app.modules.knowledge.service import KnowledgeService
from app.modules.tools.providers import default_configuration
from app.modules.tools.workspace import ToolWorkspace
from app.services.conversation_files import ConversationFiles
from app.storage.files import FileStore


def configure_foundation(
    application: FastAPI,
    resources: ApplicationResources,
    settings: Settings,
    *,
    configure_applications: bool = True,
    configure_attachments: bool = True,
) -> None:
    """平台资料始终使用新库，外部服务的模拟结果使用共享运行态。"""
    if resources.database is None:
        return
    foundation = FoundationDatabase(resources.database, settings)
    foundation.warm_up()
    application.state.foundation = foundation
    files = FileStore(foundation, resources.storage)
    application.state.file_store = files
    application.state.resource_reports = DatabaseReports(foundation, resources.storage)
    if configure_attachments:
        application.state.attachment_store = ConversationFiles(files)

    def repository(kind: str, configuration: dict | None = None) -> ResourceDatabase:
        return ResourceDatabase(
            foundation,
            kind,
            configuration=configuration,
            runtime=ResourceRuntime(resources.cache, kind) if resources.cache else None,
        )

    application.state.model_workspace = DatabaseResourceWorkspace(repository("model"))
    application.state.tool_workspace = ToolWorkspace(repository("tool", default_configuration()))
    application.state.dataset_workspace = DatasetWorkspace(
        DatasetRepository(
            foundation=foundation,
            runtime=ResourceRuntime(resources.cache, "dataset") if resources.cache else None,
        ),
        create_dataset_publisher(settings.datasets_publication_mode),
    )
    knowledge = repository("knowledge")
    application.state.knowledge_workspace = DatabaseResourceWorkspace(knowledge)
    application.state.knowledge_service = KnowledgeService(
        KnowledgeDatabase(knowledge), KnowledgeDatabaseFiles(files), RetrievalProvider(settings)
    )
    if configure_applications:
        application.state.application_service = create_database_application_service(
            resources.database, settings, cache=resources.cache, foundation=foundation
        )
