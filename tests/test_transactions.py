from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db.transactions import commit_write


def test_commit_disconnect_is_unknown_and_is_not_retried():
    session = Mock()
    session.commit.side_effect = OperationalError("COMMIT", {}, OSError("sensitive host"))
    with pytest.raises(HTTPException) as raised:
        commit_write(session)
    assert raised.value.detail == "commit_outcome_unknown"
    session.flush.assert_called_once()
    session.commit.assert_called_once()
    session.rollback.assert_called_once()


def test_flush_constraint_failure_keeps_original_error_without_committing():
    session = Mock()
    session.flush.side_effect = IntegrityError("INSERT", {}, ValueError("constraint"))
    with pytest.raises(IntegrityError):
        commit_write(session)
    session.commit.assert_not_called()


def test_successful_local_commit_does_not_retry_or_rollback():
    session = Mock()
    commit_write(session)
    session.flush.assert_called_once()
    session.commit.assert_called_once()
    session.rollback.assert_not_called()
