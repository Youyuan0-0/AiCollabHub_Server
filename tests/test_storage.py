from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from minio import Minio
from minio.error import MinioException, S3Error
from urllib3 import PoolManager

from app.storage.client import MinioStorage, ObjectNotFound, StorageUnavailable


@pytest.fixture
def storage():
    return MinioStorage(
        Mock(spec=Minio),
        bucket="platform-files",
        http=Mock(spec=PoolManager),
        max_upload_bytes=100,
    )


def test_upload_and_stat_return_metadata_without_presigned_urls(storage):
    storage.client.put_object.return_value = SimpleNamespace(etag="etag", version_id="v1")
    result = storage.put_object("files/one", BytesIO(b"data"), length=4, content_type="text/plain")
    assert result.key == "files/one" and result.size == 4
    assert result.etag == "etag" and result.version_id == "v1"
    storage.client.stat_object.return_value = SimpleNamespace(
        size=4, etag="etag", version_id="v1", content_type="text/plain"
    )
    assert storage.stat_object("files/one") == result


@pytest.mark.parametrize("key", ["", "/absolute", "../escape", "files//one", "files\\one"])
def test_object_keys_are_validated_before_sdk_calls(storage, key):
    with pytest.raises(ValueError):
        storage.put_object(key, BytesIO(b"data"), length=4)
    storage.client.put_object.assert_not_called()


@pytest.mark.parametrize("length", [-1, 101, True])
def test_upload_length_is_bounded(storage, length):
    with pytest.raises(ValueError):
        storage.put_object("files/one", BytesIO(b"data"), length=length)
    storage.client.put_object.assert_not_called()


def test_download_releases_http_connection_when_consumer_fails(storage):
    response = Mock()
    storage.client.get_object.return_value = response
    with pytest.raises(RuntimeError, match="consumer stopped"):
        with storage.open_object("files/one") as stream:
            assert stream is response
            raise RuntimeError("consumer stopped")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_download_releases_pool_connection_even_if_close_fails(storage):
    response = Mock()
    response.close.side_effect = OSError("close failed")
    storage.client.get_object.return_value = response
    with pytest.raises(OSError), storage.open_object("files/one"):
        pass
    response.release_conn.assert_called_once()


def test_missing_objects_have_a_specific_domain_error(storage):
    storage.client.stat_object.side_effect = S3Error(
        response=Mock(),
        code="NoSuchKey",
        message="private SDK response",
        resource="/private/path",
        request_id="request",
        host_id="host",
    )
    with pytest.raises(ObjectNotFound, match="object_not_found"):
        storage.stat_object("files/missing")


def test_sdk_errors_are_redacted_and_not_reported_as_success(storage, caplog):
    storage.client.put_object.side_effect = MinioException("private-signature internal-host")
    with pytest.raises(StorageUnavailable, match="object_storage_unavailable"):
        storage.put_object("files/one", BytesIO(b"data"), length=4)
    storage.client.bucket_exists.side_effect = MinioException("private-signature")
    assert not storage.probe()
    assert "private-signature" not in caplog.text
    assert "internal-host" not in caplog.text


def test_presigning_failure_uses_the_same_redacted_storage_error(storage, caplog):
    storage.client.presigned_get_object.side_effect = MinioException("private-signature")
    with pytest.raises(StorageUnavailable, match="object_storage_unavailable"):
        storage.presigned_read_url("knowledge/file", expires=timedelta(hours=24))
    assert "private-signature" not in caplog.text


def test_missing_bucket_is_not_automatically_created(storage):
    storage.client.bucket_exists.return_value = False
    assert not storage.probe()
    storage.client.make_bucket.assert_not_called()


def test_removal_and_shutdown_use_the_owned_bucket_and_pool(storage):
    storage.remove_object("files/one")
    storage.client.remove_object.assert_called_once_with("platform-files", "files/one")
    storage.close()
    storage.http.clear.assert_called_once()
