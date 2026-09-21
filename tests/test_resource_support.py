"""在隔离 schema 验证配置、可见范围和文件归档，不保留测试数据。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from test_resource_foundation import (
    MODULES,
    feedback,
    flush_constraints,
    insert,
    pending,
    ready_file,
    rejected,
    resource,
    update,
    user,
)
from test_resource_foundation import connection as connection
from test_resource_foundation import pytestmark as pytestmark
from test_resource_foundation import schema_connection as schema_connection


def get_row(connection: sa.Connection, table_name: str, identifier: UUID) -> dict:
    """读取测试创建的记录，不读取真实业务 schema。"""
    table = sa.table(table_name, sa.column("id"))
    return dict(
        connection.execute(
            sa.select(sa.literal_column("*")).select_from(table).where(table.c.id == identifier)
        )
        .mappings()
        .one()
    )


def remove(connection: sa.Connection, table_name: str, identifier: UUID) -> None:
    """只删除当前测试事务内指定 UUID 的记录。"""
    table = sa.table(table_name, sa.column("id"))
    connection.execute(table.delete().where(table.c.id == identifier))


def reference(
    connection: sa.Connection,
    module: tuple,
    owner_id: UUID,
    file_id: UUID,
    creator_id: UUID,
    **overrides,
) -> UUID:
    """生成按模块定位的文件引用，默认属于可编辑申请的使用说明。"""
    return insert(
        connection,
        "resource_file_references",
        **(
            {
                "resource_type": module[0],
                "owner_type": "publication",
                "owner_id": owner_id,
                "file_id": file_id,
                "creator_id": creator_id,
                "purpose": "tutorial",
            }
            | overrides
        ),
    )


def draft(connection: sa.Connection, module: tuple, owner: UUID) -> UUID:
    """保存申请草稿，便于在提交冻结之前登记文件。"""
    return insert(
        connection,
        f"{module[2]}_publications",
        applicant_id=owner,
        version="1.0",
        data=Jsonb({"name": "文件归档验证"}),
    )


def submit(connection: sa.Connection, module: tuple, identifier: UUID) -> None:
    """用当前修订和递增轮次提交申请。"""
    table = f"{module[2]}_publications"
    row = get_row(connection, table, identifier)
    update(
        connection,
        table,
        identifier,
        review_status="pending",
        revision=row["revision"] + 1,
        submission_no=row["submission_no"] + 1,
        submitted_at=datetime.now(UTC),
    )
    flush_constraints(connection)


def archive_review(
    connection: sa.Connection,
    module: tuple,
    publication_id: UUID,
    reviewer: UUID,
    *,
    decision: str = "approved",
    reports: tuple = (),
    archive_tutorials: bool = True,
    consume_reports: bool = True,
) -> UUID:
    """同一事务先归档审核文件，再更新结论，验证正式历史不可补写。"""
    table = f"{module[2]}_publications"
    row = get_row(connection, table, publication_id)
    review_id = insert(
        connection,
        "resource_reviews",
        resource_type=module[0],
        publication_id=publication_id,
        submission_no=row["submission_no"],
        reviewer_id=reviewer,
        decision=decision,
        comment="需要补充资料" if decision == "rejected" else None,
        submitted_at=row["submitted_at"],
        submission_snapshot=Jsonb(
            {"version": row["version"], "versionNotes": row["version_notes"], "data": row["data"]}
        ),
    )
    if archive_tutorials:
        copy_tutorials(connection, module, publication_id, review_id, "review", reviewer)
    for report_id in reports:
        staged = get_row(connection, "resource_file_references", report_id)
        reference(
            connection,
            module,
            review_id,
            staged["file_id"],
            reviewer,
            owner_type="review",
            purpose="review_report",
        )
        if consume_reports:
            remove(connection, "resource_file_references", report_id)
    update(connection, table, publication_id, review_status=decision, revision=row["revision"] + 1)
    flush_constraints(connection)
    return review_id


def copy_tutorials(
    connection: sa.Connection,
    module: tuple,
    publication_id: UUID,
    owner_id: UUID,
    owner_type: str,
    creator: UUID,
) -> None:
    """按申请引用生成不可变审核或版本引用，不复制文件本体。"""
    files = (
        connection.execute(
            sa.text(
                "SELECT file_id FROM resource_file_references WHERE resource_type=:kind "
                "AND owner_type='publication' AND owner_id=:id AND purpose='tutorial'"
            ),
            {"kind": module[0], "id": publication_id},
        )
        .scalars()
        .all()
    )
    for file_id in files:
        reference(connection, module, owner_id, file_id, creator, owner_type=owner_type)


def publish_version(
    connection: sa.Connection,
    module: tuple,
    publication_id: UUID,
    owner: UUID,
    *,
    archive_tutorials: bool = True,
) -> UUID:
    """正式版本和引用完成后再回填发布结果，最后验证同一事务的一致性。"""
    kind, main_table, prefix = module
    row = get_row(connection, f"{prefix}_publications", publication_id)
    resource_id = resource(connection, module, owner)
    moment = datetime.now(UTC)
    version_id = insert(
        connection,
        f"{prefix}_versions",
        **{f"{prefix}_id": resource_id},
        publication_id=publication_id,
        version=row["version"],
        snapshot=Jsonb(row["data"]),
        publisher_id=owner,
        published_at=moment,
    )
    if archive_tutorials:
        copy_tutorials(connection, module, publication_id, version_id, "version", owner)
    update(
        connection,
        f"{prefix}_publications",
        publication_id,
        **{f"{prefix}_id": resource_id},
        published_version_id=version_id,
        published_at=moment,
        revision=row["revision"] + 1,
    )
    update(connection, main_table, resource_id, current_version_id=version_id)
    flush_constraints(connection)
    return version_id


def test_configuration_unique_revision_and_stale_save(connection):
    """同类配置唯一，只有匹配旧修订号的保存可以生效。"""
    owner = user(connection)
    values = {"module_code": "applications", "config_key": "form", "value": Jsonb({})}
    identifier = insert(connection, "module_configurations", **values)
    with rejected(connection):
        insert(connection, "module_configurations", **values)
    with rejected(connection):
        update(connection, "module_configurations", identifier, value=Jsonb({"fieldHints": {}}))
    update(
        connection,
        "module_configurations",
        identifier,
        value=Jsonb({"fieldHints": {}}),
        revision=2,
        updater_id=owner,
    )
    stale = connection.execute(
        sa.text("UPDATE module_configurations SET revision=2 WHERE id=:id AND revision=1"),
        {"id": identifier},
    )
    assert stale.rowcount == 0
    with rejected(connection):
        update(connection, "module_configurations", identifier, module_code="tools", revision=3)
    assert get_row(connection, "module_configurations", identifier)["updater_id"] == owner


@pytest.mark.parametrize(
    "values",
    [
        {"module_code": "unknown"},
        {"config_key": "password"},
        {"value": Jsonb([])},
        {"revision": 2},
        {"updater_id": uuid4()},
    ],
)
def test_configuration_invalid_values(connection, values):
    """拒绝不支持的模块、配置类别、内容类型和无效操作人。"""
    with rejected(connection):
        insert(
            connection,
            "module_configurations",
            **(
                {
                    "module_code": "applications",
                    "config_key": "workflow",
                    "value": Jsonb({}),
                }
                | values
            ),
        )


@pytest.mark.parametrize("module", MODULES)
def test_visibility_switch_is_atomic_and_keeps_resource_reference(connection, module):
    """五类资源都支持范围切换，空名单和悬空资源不能提交。"""
    owner = user(connection)
    resource_id = resource(connection, module, owner)
    policy = insert(
        connection,
        "resource_visibility_policies",
        resource_type=module[0],
        resource_id=resource_id,
        scope="all",
    )
    with rejected(connection):
        insert(
            connection,
            "resource_visibility_policies",
            resource_type=module[0],
            resource_id=resource_id,
            scope="all",
        )
    with rejected(connection):
        remove(connection, module[1], resource_id)
    with rejected(connection):
        update(connection, "resource_visibility_policies", policy, scope="members", revision=2)
    update(
        connection,
        "resource_visibility_policies",
        policy,
        scope="members",
        revision=2,
        updater_id=owner,
    )
    target = insert(connection, "resource_visibility_targets", policy_id=policy, user_id=owner)
    flush_constraints(connection)
    with rejected(connection):
        insert(connection, "resource_visibility_targets", policy_id=policy, user_id=owner)
    with rejected(connection):
        remove(connection, "resource_visibility_targets", target)
    with rejected(connection):
        update(connection, "resource_visibility_policies", policy, scope="all", revision=3)
    remove(connection, "resource_visibility_targets", target)
    update(connection, "resource_visibility_policies", policy, scope="all", revision=3)
    flush_constraints(connection)


def test_visibility_rejects_mock_scope_and_missing_resource_or_user(connection):
    """不把 Mock 组织、项目和未知 UUID 保存为真实授权。"""
    owner = user(connection)
    resource_id = resource(connection, MODULES[2], owner)
    values = {"resource_type": "application", "resource_id": resource_id, "scope": "all"}
    for invalid in (
        {"scope": "organizations"},
        {"resource_id": uuid4()},
        {"resource_type": "dataset"},
    ):
        with rejected(connection):
            insert(connection, "resource_visibility_policies", **(values | invalid))
    policy = insert(connection, "resource_visibility_policies", **values)
    with rejected(connection):
        insert(connection, "resource_visibility_targets", policy_id=policy, user_id=uuid4())
    with rejected(connection):
        insert(connection, "resource_visibility_targets", policy_id=policy, user_id=owner)


@pytest.mark.parametrize("module", MODULES)
def test_tutorial_and_report_archive_for_all_modules(connection, module):
    """贯穿草稿、提交、审核和发布，归档之后不能追加或清理历史附件。"""
    owner = user(connection)
    publication = draft(connection, module, owner)
    file_id = ready_file(connection, owner)
    reference_id = reference(connection, module, publication, file_id, owner)
    submit(connection, module, publication)
    with rejected(connection):
        remove(connection, "resource_file_references", reference_id)
    with rejected(connection):
        reference(connection, module, publication, ready_file(connection, owner), owner)
    staged = reference(
        connection,
        module,
        publication,
        ready_file(connection, owner),
        owner,
        purpose="review_report",
        submission_no=1,
    )
    review_id = archive_review(connection, module, publication, owner, reports=(staged,))
    version_id = publish_version(connection, module, publication, owner)
    assert (
        connection.execute(
            sa.text("SELECT count(*) FROM resource_file_references WHERE file_id=:id"),
            {"id": file_id},
        ).scalar_one()
        == 3
    )
    for owner_type, identifier in (("review", review_id), ("version", version_id)):
        with rejected(connection):
            reference(
                connection,
                module,
                identifier,
                ready_file(connection, owner),
                owner,
                owner_type=owner_type,
            )
        historical = connection.execute(
            sa.text(
                "SELECT id FROM resource_file_references WHERE owner_type=:kind AND owner_id=:id "
                "AND purpose='tutorial'"
            ),
            {"kind": owner_type, "id": identifier},
        ).scalar_one()
        with rejected(connection):
            remove(connection, "resource_file_references", historical)
        with rejected(connection):
            update(connection, "resource_file_references", historical, file_id=uuid4())
    with rejected(connection):
        update(connection, "files", file_id, status="deleting")
    with rejected(connection):
        update(connection, "files", file_id, expires_at=datetime.now(UTC))
    assert get_row(connection, "files", file_id)["expires_at"] is None


def test_archiving_cannot_omit_tutorials_or_leave_selected_reports_staged(connection):
    """遗漏归档或未转移选中报告时整个审核事务失败，不留下半成品。"""
    module = MODULES[2]
    owner = user(connection)
    publication = draft(connection, module, owner)
    reference(connection, module, publication, ready_file(connection, owner), owner)
    submit(connection, module, publication)
    staged = reference(
        connection,
        module,
        publication,
        ready_file(connection, owner),
        owner,
        purpose="review_report",
        submission_no=1,
    )
    with rejected(connection):
        archive_review(connection, module, publication, owner, archive_tutorials=False)
    with rejected(connection):
        archive_review(
            connection, module, publication, owner, reports=(staged,), consume_reports=False
        )
    assert connection.exec_driver_sql("SELECT count(*) FROM resource_reviews").scalar_one() == 0
    archive_review(connection, module, publication, owner, reports=(staged,))
    with rejected(connection):
        publish_version(connection, module, publication, owner, archive_tutorials=False)
    publish_version(connection, module, publication, owner)


def test_reports_enforce_reviewer_round_limit_and_no_reuse(connection):
    """限制每人每轮三份报告，拒绝使用他人、旧轮次或已经归档的文件。"""
    module = MODULES[2]
    owner, another = user(connection), user(connection)
    publication = pending(connection, module, owner)
    staged = [
        reference(
            connection,
            module,
            publication,
            ready_file(connection, owner),
            owner,
            purpose="review_report",
            submission_no=1,
        )
        for _ in range(3)
    ]
    with rejected(connection):
        reference(
            connection,
            module,
            publication,
            ready_file(connection, owner),
            owner,
            purpose="review_report",
            submission_no=1,
        )
    another_report = reference(
        connection,
        module,
        publication,
        ready_file(connection, another),
        another,
        purpose="review_report",
        submission_no=1,
    )
    with rejected(connection):
        archive_review(connection, module, publication, owner, reports=(another_report,))
    with rejected(connection):
        reference(
            connection,
            module,
            publication,
            ready_file(connection, another),
            owner,
            purpose="review_report",
            submission_no=1,
        )
    used_file = get_row(connection, "resource_file_references", staged[0])["file_id"]
    archive_review(
        connection, module, publication, owner, decision="rejected", reports=(staged[0],)
    )
    submit(connection, module, publication)
    with rejected(connection):
        archive_review(connection, module, publication, owner, reports=(staged[1],))
    with rejected(connection):
        reference(
            connection,
            module,
            publication,
            ready_file(connection, owner),
            owner,
            purpose="review_report",
            submission_no=1,
        )
    with rejected(connection):
        reference(
            connection,
            module,
            publication,
            used_file,
            owner,
            purpose="review_report",
            submission_no=2,
        )
    reference(
        connection,
        module,
        publication,
        ready_file(connection, owner),
        owner,
        purpose="review_report",
        submission_no=2,
    )


def test_rejected_draft_edit_keeps_previous_files_protected(connection):
    """驳回后替换说明文件不影响上一轮审核的文件保留。"""
    module = MODULES[2]
    owner = user(connection)
    publication = draft(connection, module, owner)
    original = ready_file(connection, owner)
    draft_reference = reference(connection, module, publication, original, owner)
    submit(connection, module, publication)
    archive_review(connection, module, publication, owner, decision="rejected")
    remove(connection, "resource_file_references", draft_reference)
    reference(connection, module, publication, ready_file(connection, owner), owner)
    with rejected(connection):
        update(connection, "files", original, status="deleting")
    assert connection.execute(
        sa.text("SELECT owner_type FROM resource_file_references WHERE file_id=:id"),
        {"id": original},
    ).scalars().all() == ["review"]
    submit(connection, module, publication)
    archive_review(connection, module, publication, owner)
    publish_version(connection, module, publication, owner)


@pytest.mark.parametrize(
    "invalid",
    [
        {"owner_id": uuid4()},
        {"resource_type": "dataset"},
        {"purpose": "unknown"},
        {"submission_no": 1},
        {"file_id": uuid4()},
    ],
)
def test_resource_file_rejects_invalid_ownership(connection, invalid):
    """多态关联仍检查实际所属模块、用途、轮次与文件存在性。"""
    owner = user(connection)
    publication = draft(connection, MODULES[2], owner)
    with rejected(connection):
        insert(
            connection,
            "resource_file_references",
            **(
                {
                    "resource_type": "application",
                    "owner_type": "publication",
                    "owner_id": publication,
                    "file_id": ready_file(connection, owner),
                    "creator_id": owner,
                    "purpose": "tutorial",
                }
                | invalid
            ),
        )


def test_draft_delete_requires_detaching_files_and_reference_requires_ready_file(connection):
    """草稿删除前解除引用，上传失败或已过期的文件不能挂载。"""
    owner = user(connection)
    publication = draft(connection, MODULES[2], owner)
    for values in ({"status": "failed"}, {"expires_at": datetime.now(UTC) - timedelta(hours=1)}):
        with rejected(connection):
            reference(
                connection, MODULES[2], publication, ready_file(connection, owner, **values), owner
            )
    file_id = ready_file(connection, owner)
    identifier = reference(connection, MODULES[2], publication, file_id, owner)
    with rejected(connection):
        remove(connection, "application_publications", publication)
    remove(connection, "resource_file_references", identifier)
    remove(connection, "application_publications", publication)
    update(connection, "files", file_id, status="deleting")


def test_preview_mapping_inherits_original_reference_and_protects_both_files(connection):
    """预览不能被独立挂成业务附件，原件在用时双方均受清理保护。"""
    owner = user(connection)
    original, preview = ready_file(connection, owner), ready_file(connection, owner)
    mapping = insert(connection, "file_previews", source_file_id=original, preview_file_id=preview)
    publication = draft(connection, MODULES[2], owner)
    reference_id = reference(connection, MODULES[2], publication, original, owner)
    with rejected(connection):
        reference(connection, MODULES[2], publication, preview, owner)
    with rejected(connection):
        feedback(connection, owner, file_ids=[preview])
    for file_id in (original, preview):
        assert get_row(connection, "files", file_id)["expires_at"] is None
        with rejected(connection):
            update(connection, "files", file_id, status="deleting")
        with rejected(connection):
            remove(connection, "files", file_id)
    with rejected(connection):
        remove(connection, "file_previews", mapping)
    remove(connection, "resource_file_references", reference_id)
    remove(connection, "file_previews", mapping)
    for file_id in (original, preview):
        update(connection, "files", file_id, status="deleting")


def test_previews_reject_duplicate_self_chain_cycle_unready_and_repurpose(connection):
    """映射只能连接两个独立就绪文件，不能改变已有业务文件的权限归属。"""
    owner = user(connection)
    original, preview, third = [ready_file(connection, owner) for _ in range(3)]
    mapping = insert(connection, "file_previews", source_file_id=original, preview_file_id=preview)
    for source, target in (
        (original, original),
        (original, third),
        (third, preview),
        (preview, third),
        (third, original),
        (preview, original),
        (third, ready_file(connection, owner, status="uploading")),
    ):
        with rejected(connection):
            insert(connection, "file_previews", source_file_id=source, preview_file_id=target)
    with rejected(connection):
        update(connection, "file_previews", mapping, preview_file_id=third)
    feedback(connection, owner, file_ids=[third])
    with rejected(connection):
        insert(
            connection,
            "file_previews",
            source_file_id=ready_file(connection, owner),
            preview_file_id=third,
        )
