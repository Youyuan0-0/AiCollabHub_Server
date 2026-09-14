from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.db.transactions import commit_write


def test_commit_warning_does_not_report_synchronous_success():
    session = Mock()
    driver = session.connection.return_value.connection.driver_connection
    handlers = []
    driver.add_notice_handler.side_effect = handlers.append

    def commit():
        notice = Mock(severity_nonlocalized="WARNING", sqlstate="01000")
        handlers[0](notice)

    session.commit.side_effect = commit
    with pytest.raises(HTTPException) as raised:
        commit_write(session)
    assert raised.value.status_code == 503
    assert raised.value.detail == "commit_outcome_unknown"
    driver.remove_notice_handler.assert_called_once_with(handlers[0])


def test_commit_disconnect_is_unknown_and_is_not_retried():
    session = Mock()
    session.commit.side_effect = OperationalError("COMMIT", {}, OSError("sensitive host"))
    with pytest.raises(HTTPException) as raised:
        commit_write(session)
    assert raised.value.detail == "commit_outcome_unknown"
    session.commit.assert_called_once()
    session.rollback.assert_called_once()


def test_commit_succeeds_without_warning_and_releases_notice_handler():
    session = Mock()
    commit_write(session)
    session.connection.return_value.connection.driver_connection.remove_notice_handler.assert_called_once()
