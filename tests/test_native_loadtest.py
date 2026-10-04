"""验证压测会话、业务成功判定及速率/并发含义，避免错误的容量结论。"""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def loadtest():
    source = Path(__file__).resolve().parents[1] / "deploy/native/loadtest/loadtest.py"
    spec = importlib.util.spec_from_file_location("native_loadtest_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("body", [b"<html>Login</html>", b'{"error":"denied"}', b"null", b"[]"])
def test_login_page_or_error_cannot_be_counted_as_business_success(loadtest, body):
    assert not loadtest.valid_body(body, "page")


def test_expected_empty_catalog_is_valid_and_knowledge_allows_list(loadtest):
    assert loadtest.valid_body(b'{"items":[],"total":0}', "page")
    assert loadtest.valid_body(b"[]", "list_or_page")


@pytest.mark.parametrize("cookie", ["", "a\nb", "a;b", "a,b", 'a"b', "a b"])
def test_bad_cookie_rejected_before_sending_requests(loadtest, tmp_path, cookie):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps([{"subject": "tester", "cookie": cookie}]))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="无效身份"):
        loadtest.read_identities(path)


def test_duplicate_identity_rejected(loadtest, tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps([{"subject": "tester", "cookie": "opaque-token"}] * 2))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="不能重复"):
        loadtest.read_identities(path)


def test_fixed_concurrency_is_unlimited_rate_and_rate_mode_has_worker_limit(loadtest):
    args = argparse.Namespace(
        profile="custom", rate=None, concurrency=5000, workers=100, duration=60
    )
    assert loadtest.build_cases(args) == [("custom-concurrency", 0, 5000, 5000, 60)]
    args.rate, args.concurrency, args.workers = 200, None, 50
    assert loadtest.build_cases(args) == [("custom-rate", 200, 10, 50, 60)]


def test_custom_requires_explicit_load_and_baseline_cannot_ignore_it(loadtest):
    args = argparse.Namespace(profile="custom", rate=None, concurrency=None)
    with pytest.raises(ValueError, match="必须选择"):
        loadtest.build_cases(args)
    args.profile, args.rate = "baseline", 1000
    with pytest.raises(ValueError, match="只能搭配"):
        loadtest.build_cases(args)


def test_cookie_never_follows_redirect(loadtest):
    assert (
        loadtest.NoRedirect().redirect_request(None, None, 302, "", {}, "http://other.test") is None
    )
