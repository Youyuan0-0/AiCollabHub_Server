import importlib.metadata
import os
import platform
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


def pytest_addoption(parser):
    group = parser.getgroup("dameng")
    group.addoption("--dm-driver-dir", help="Directory containing isolated Dameng packages")
    group.addoption("--dm-live", action="store_true", help="Use a dedicated Dameng test database")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "dm_live: requires a dedicated writable Dameng test database"
    )
    driver_dir = config.getoption("--dm-driver-dir")
    if driver_dir:
        path = Path(driver_dir).resolve()
        if not path.is_dir():
            raise pytest.UsageError("--dm-driver-dir must be an existing directory")
        # pip --target 隔离驱动，避免为了验证修改现有开发环境或项目依赖。
        sys.path.insert(0, str(path))
    if config.getoption("--dm-live"):
        value = os.environ.get("DM_TEST_DATABASE_URL", "")
        if not value:
            raise pytest.UsageError("--dm-live requires DM_TEST_DATABASE_URL")
        try:
            url = make_url(value)
        except Exception:
            raise pytest.UsageError("DM_TEST_DATABASE_URL is not a valid SQLAlchemy URL") from None
        if url.drivername != "dm+dmPython":
            raise pytest.UsageError("DM_TEST_DATABASE_URL must use dm+dmPython")
        if not url.username or url.username.upper() in {"SYSDBA", "SYSAUDITOR"}:
            raise pytest.UsageError("Use a dedicated test account, not a system account")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--dm-live"):
        skip = pytest.mark.skip(reason="No --dm-live: real Dameng behavior has not been verified")
        for item in items:
            if item.get_closest_marker("dm_live"):
                item.add_marker(skip)


def pytest_report_header():
    versions = []
    for name in ("SQLAlchemy", "alembic", "dmPython", "dmSQLAlchemy"):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = "NOT INSTALLED"
        versions.append(f"{name}={version}")
    return [f"Dameng probe: Python {platform.python_version()} / {platform.machine()}", *versions]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    # 在线连接异常的 traceback 可能包含驱动参数；报告只保留类型和数值错误码。
    if report.failed and call.excinfo and item.get_closest_marker("dm_live"):
        error = call.excinfo.value
        original = getattr(error, "orig", error)
        args = getattr(original, "args", ())
        # dmPython 把数值错误码放在首个错误对象的 code 属性中。
        first = args[0] if args else None
        code = first if isinstance(first, int) else getattr(first, "code", None)
        if not isinstance(code, int):
            code = None
        report.longrepr = (
            f"{item.nodeid}: {type(error).__name__}, database_error_code={code}. "
            "Connection parameters and raw database exception details are omitted."
        )
