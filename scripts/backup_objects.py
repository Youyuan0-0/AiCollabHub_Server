"""Exercise object backup and isolated restore without a business file table."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from minio.helpers import check_bucket_name

from app.core.config import Settings
from app.core.resources import create_resources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prefix", default="framework-probes/")
    parser.add_argument("--restore-bucket", help="New verification bucket; never the source")
    args = parser.parse_args()
    if not args.prefix or not args.prefix.endswith("/"):
        parser.error("An explicit nonempty object prefix ending in / is required")
    resources = create_resources(Settings())
    try:
        if not resources.storage:
            parser.error("MinIO must be configured")
        storage = resources.storage
        if args.restore_bucket:
            check_bucket_name(args.restore_bucket, strict=True)
            if args.restore_bucket == storage.bucket or storage.client.bucket_exists(
                args.restore_bucket
            ):
                parser.error("Restore verification requires a new bucket")
        target = args.directory.resolve() / ("objects_" + uuid4().hex)
        target.mkdir(parents=True)
        manifest = {
            "bucket": storage.bucket,
            "prefix": args.prefix,
            "objects": [],
            "complete": False,
            "restoreVerified": False,
        }
        for record in storage.client.list_objects(
            storage.bucket, prefix=args.prefix, recursive=True
        ):
            filename = uuid4().hex + ".bin"
            digest = hashlib.sha256()
            size = 0
            # 本地文件名独立生成，不把对象 key 或上传文件名拼接为磁盘路径。
            with (
                storage.open_object(record.object_name) as response,
                (target / filename).open("wb") as file,
            ):
                for chunk in response.stream(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    file.write(chunk)
            if size != record.size:
                raise RuntimeError("Object changed during backup")
            manifest["objects"].append(
                {
                    "objectKey": record.object_name,
                    "file": filename,
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
            )
        if args.restore_bucket:
            if not manifest["objects"]:
                parser.error("No test objects were found; an empty restore is not verification")
            storage.client.make_bucket(args.restore_bucket)
            for item in manifest["objects"]:
                with (target / item["file"]).open("rb") as file:
                    storage.client.put_object(
                        args.restore_bucket, item["objectKey"], file, item["size"]
                    )
                response = storage.client.get_object(args.restore_bucket, item["objectKey"])
                digest = hashlib.sha256()
                try:
                    for chunk in response.stream(1024 * 1024):
                        digest.update(chunk)
                finally:
                    response.close()
                    response.release_conn()
                if digest.hexdigest() != item["sha256"]:
                    raise RuntimeError("Restored object checksum mismatch")
            manifest.update(restoreVerified=True, restoreBucket=args.restore_bucket)
        manifest["complete"] = True
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            f"Backed up {len(manifest['objects'])} objects. Restore: {manifest['restoreVerified']}"
        )
    finally:
        resources.close()


if __name__ == "__main__":
    main()
