"""提交轮次、质量评分与模拟外部状态的组合；不复制外部版本或完整草稿。"""

import json
from copy import deepcopy
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import insert, update

from app.modules.applications.database_values import current_time, date_value, iso, stable_id
from app.modules.applications.demo_marketplace import LOCAL_FIELDS


class DatabasePublicationMixin:
    def extension_rows(self, publication_id):
        table = self.t("submission_extension")
        return sorted(
            self.rows("submission_extension", table.c.submission_key == publication_id),
            key=lambda row: row["round_no"],
        )

    def list_publications(self):
        table = self.t("submission_extension")
        ids = self.db.execute(table.select().with_only_columns(table.c.submission_key).distinct())
        return [
            record for key in sorted(row[0] for row in ids) if (record := self.get_publication(key))
        ]

    def publication_data(self, extension):
        external = self.marketplace.load(extension["external_version_id"], "submission")
        external["data"].update(deepcopy(extension["local_fields"]))
        return external

    def get_publication(self, publication_id):
        self.lock("publication:" + publication_id)
        extensions = self.extension_rows(publication_id)
        if not extensions:
            return None
        latest = extensions[-1]
        record = self.publication_data(latest)
        application = self.one(
            "application", self.t("application").c.id == latest["application_id"]
        )
        record.update(
            id=publication_id,
            applicationId=latest["application_id"],
            revision=latest["revision"],
            updatedAt=iso(latest["updated_at"]),
            createdBy=latest["created_by_subject"],
            createdByName=self.display_name(latest["created_by_subject"]),
            organizationId=application["organization_id"] or "",
            securityLevel=latest["security_level"],
            priority=latest["priority"],
            submittedAt=iso(latest["frozen_at"]),
            reviews=[],
        )
        if latest["form_rules_snapshot"]:
            record["submissionRules"] = deepcopy(latest["form_rules_snapshot"])
            record["formRevision"] = latest["form_rules_snapshot"].get("revision")
        note_table = self.t("review_note")
        for extension in extensions:
            note = self.one("review_note", note_table.c.submission_extension_id == extension["id"])
            if note is None:
                continue
            decision = self.one("activity", self.t("activity").c.id == note["decision_activity_id"])
            # 本地建议不能充当外部最终状态；开发模式也从独立模拟结果读取状态。
            if decision is None or decision["status"] != "succeeded":
                continue
            result = self.marketplace.load(decision["external_request_id"], "decision")
            if extension["id"] == latest["id"]:
                record["status"] = result["status"]
            scores = None
            if note["completeness_score"] is not None:
                scores = {
                    "completeness": note["completeness_score"],
                    "usability": note["usability_score"],
                    "security": note["security_score"],
                }
            record["reviews"].append(
                {
                    "id": note["id"],
                    "decision": note["recommendation"],
                    "reason": note["reason"],
                    "reviewerId": note["reviewer_subject"],
                    "reviewerName": self.display_name(note["reviewer_subject"]),
                    "scores": scores,
                    "reviewedAt": iso(note["reviewed_at"]),
                    "submittedAt": iso(extension["frozen_at"]),
                    "data": self.publication_data(extension)["data"],
                }
            )
        return record

    def put_publication(self, record):
        self.lock("publication:" + record["id"])
        self.ensure_application(
            record["applicationId"],
            {
                "createdBy": record["createdBy"],
                "organizationId": record.get("organizationId", ""),
                "createdAt": record["createdAt"],
            },
            published=False,
        )
        rows = self.extension_rows(record["id"])
        previous = rows[-1] if rows else None
        table = self.t("submission_extension")
        if record["status"] in ("approved", "rejected", "needs_changes"):
            if previous is None or previous["frozen_at"] is None:
                raise HTTPException(409, "申请尚未提交")
            self.save_review(record, previous)
            self.write(
                update(table)
                .where(table.c.id == previous["id"])
                .values(
                    revision=record["revision"],
                    updated_at=date_value(record["updatedAt"]),
                )
            )
            return
        new_round = previous is None or previous["frozen_at"] is not None
        round_no = (
            (previous["round_no"] + 1 if previous else 1) if new_round else previous["round_no"]
        )
        extension_id = stable_id("extension", f"{record['id']}:{round_no}")
        snapshot = self.marketplace.save_submission(record)
        values = {
            "application_id": record["applicationId"],
            "submission_key": record["id"],
            "round_no": round_no,
            "created_by_subject": record["createdBy"],
            "market_profile": self.marketplace.profile,
            "external_version_id": snapshot,
            "external_submission_id": "demo-submission:" + extension_id
            if record["status"] == "pending"
            else None,
            "local_fields": {
                key: deepcopy(record["data"][key]) for key in LOCAL_FIELDS if key in record["data"]
            },
            "form_rules_snapshot": deepcopy(record.get("submissionRules", {}))
            if record["status"] == "pending"
            else {},
            "security_level": record["securityLevel"],
            "priority": record["priority"],
            "frozen_at": date_value(record["submittedAt"])
            if record["status"] == "pending"
            else None,
            "revision": record["revision"],
            "updated_at": date_value(record["updatedAt"]),
        }
        if new_round:
            self.write(insert(table).values(id=extension_id, **values))
        else:
            if previous["application_id"] != record["applicationId"]:
                raise HTTPException(409, "申请不能改挂其他应用")
            self.write(update(table).where(table.c.id == previous["id"]).values(**values))
        source = record.get("source", {})
        if source.get("kind") == "toolchain":
            application = self.t("application")
            self.write(
                update(application)
                .where(application.c.id == record["applicationId"])
                .values(
                    agent_profile=source["provider"],
                    workspace_code=source["workspace"],
                    agent_code=source["externalId"],
                    updated_at=current_time(),
                )
            )

    def save_review(self, record, extension):
        review = record["reviews"][-1]
        activity_id = stable_id("review-decision", review["id"])
        if self.one("review_note", self.t("review_note").c.id == review["id"]):
            raise HTTPException(409, "本轮申请已经审核")
        policy = self.configuration_record("workflow")
        scores = review["scores"] or {}
        external_result = self.marketplace.save(
            "decision",
            {
                "status": record["status"],
                "submissionId": extension["external_submission_id"],
            },
        )
        fingerprint = sha256(
            json.dumps(
                {
                    "submission": extension["id"],
                    "reviewer": review["reviewerId"],
                    "decision": review["decision"],
                    "reason": review["reason"],
                    "mode": "simulation",
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        self.write(
            insert(self.t("activity")).values(
                id=activity_id,
                application_id=record["applicationId"],
                submission_extension_id=extension["id"],
                operation="review_decision",
                actor_subject=review["reviewerId"],
                request_id="review:" + review["id"],
                request_fingerprint=fingerprint,
                mode="simulation",
                status="succeeded",
                provider_profile=self.marketplace.profile,
                external_request_id=external_result,
                external_submission_id=extension["external_submission_id"],
                created_at=date_value(review["reviewedAt"]),
                completed_at=date_value(review["reviewedAt"]),
            )
        )
        self.write(
            insert(self.t("review_note")).values(
                id=review["id"],
                application_id=record["applicationId"],
                submission_extension_id=extension["id"],
                decision_activity_id=activity_id,
                reviewer_subject=review["reviewerId"],
                recommendation=review["decision"],
                reason=review["reason"],
                completeness_score=scores.get("completeness"),
                usability_score=scores.get("usability"),
                security_score=scores.get("security"),
                minimum_score=policy["minimumQualityScore"],
                policy_snapshot={
                    key: policy[key] for key in ("minimumQualityScore", "sensitiveTerms")
                },
                evidence_snapshot={"externalVersionId": extension["external_version_id"]},
                reviewed_at=date_value(review["reviewedAt"]),
            )
        )
