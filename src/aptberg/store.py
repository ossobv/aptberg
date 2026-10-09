"""The S3 store: the only module that imports boto3

Moves bytes and answers "does key K hold sha256 H". It knows nothing
about suites, channels or snapshots.

Every object we write carries its sha256 in the x-amz-meta-sha256
header, which is what "does K hold H" compares.

Everything up to 5 GiB (the S3 single PUT limit; the largest .deb is
under 3) is uploaded with one PUT, never multipart, so that its ETag is
the MD5 of its content, computed by the server. A multipart upload's
ETag is md5(part md5s)-N and says nothing about the whole object.
"""

import base64
import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore import handlers
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ParamValidationError,
)

from . import user_agent

SINGLE_PUT_MAX = 5 << 30

TRANSFER = TransferConfig(
    multipart_threshold=SINGLE_PUT_MAX,
    multipart_chunksize=64 << 20,
    max_concurrency=4,
    use_threads=True,
)

CONTENT_TYPES = {
    '.json': 'application/json',
    '.deb': 'application/vnd.debian.binary-package',
    '.udeb': 'application/vnd.debian.binary-package',
    '.ddeb': 'application/vnd.debian.binary-package',
}


# Ceph RGW names the buckets of a tenant "tenant:bucket". botocore's own
# check (handlers.VALID_BUCKET) refuses the colon, so our client swaps it
# for this one; other botocore users in the process are unaffected. Such
# a name is not DNS-safe, so botocore addresses it path-style, with the
# colon percent-encoded.
_BUCKET = re.compile(r'(?:[a-zA-Z0-9_-]+:)?[a-zA-Z0-9.\-_]{1,255}')


def _validate_bucket(params, **kwargs) -> None:
    bucket = params.get('Bucket')
    if bucket is not None and not _BUCKET.fullmatch(bucket):
        raise ParamValidationError(
            report=f'Invalid bucket name {bucket!r}: expected [tenant:]bucket'
        )


class IntegrityError(Exception):
    "The server's view of an object does not match what we sent"


class StoreError(Exception):
    "The bucket is not usable: bad credentials, bad bucket, unreachable"


@contextmanager
def _wrapped(desc: str):
    "Turn a botocore failure into a StoreError naming the op and key(s)"
    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        raise StoreError(f'{desc}: {exc}') from exc


class Store:
    "One bucket"

    def __init__(
        self,
        bucket: str,
        endpoint: str | None = None,
        region: str | None = None,
        concurrency: int = 8,
        agent: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.concurrency = concurrency
        self.s3 = boto3.client(
            's3',
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                retries={'mode': 'adaptive', 'max_attempts': 10},
                # boto3's own User-Agent, with ours appended
                user_agent_extra=agent or user_agent(),
                max_pool_connections=max(64, concurrency * 8),
                # boto3 >= 1.36 adds CRC checksums to every upload by
                # default, which older Ceph RGW releases reject.
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required',
            ),
        )
        events = self.s3.meta.events
        events.unregister(
            'before-parameter-build.s3', handlers.validate_bucket_name
        )
        events.register('before-parameter-build.s3', _validate_bucket)

    def check(self) -> None:
        """One cheap call, to fail on bad credentials before expensive work

        Callers with a lot of local work ahead of their first real S3
        call (fetch verifies and filters a whole archive before it
        uploads anything) should call this first, so a credentials or
        bucket misconfiguration surfaces in seconds, not after that
        work is done.
        """
        with _wrapped(f'cannot use s3://{self.bucket}'):
            self.s3.list_objects_v2(Bucket=self.bucket, MaxKeys=1)

    def list_sizes(self, prefix: str) -> dict[str, int]:
        "Every key under prefix with its size"
        return {obj['Key']: obj['Size'] for obj in self._objects(prefix)}

    def sha256(self, key: str) -> str | None:
        """The sha256 recorded on key; None if absent

        An object without our metadata was not written by aptberg and
        yields the empty string, which matches no digest.
        """
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _absent(exc):
                return None
            raise
        return head['Metadata'].get('sha256', '')

    def list_etags(self, prefix: str) -> dict[str, tuple[int, str]]:
        """Every key under prefix with its size and ETag (unquoted)

        A single PUT's ETag is the MD5 of the content; a multipart one
        ends in -<parts>.
        """
        return {
            obj['Key']: (obj['Size'], obj['ETag'].strip('"'))
            for obj in self._objects(prefix)
        }

    def list_objects(self, prefix: str) -> dict[str, tuple[int, datetime]]:
        "Every key under prefix with its size and last modification"
        return {
            obj['Key']: (obj['Size'], obj['LastModified'])
            for obj in self._objects(prefix)
        }

    def _objects(self, prefix: str) -> Iterator[dict]:
        "Every object under prefix, as list_objects_v2 describes it"
        pages = self.s3.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        for page in pages:
            yield from page.get('Contents', ())

    def list_dirs(self, prefix: str) -> list[str]:
        "The names one level below prefix (which ends in a slash)"
        out = []
        pages = self.s3.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter='/'
        )
        for page in pages:
            for common in page.get('CommonPrefixes', ()):
                out.append(common['Prefix'][len(prefix) :].rstrip('/'))
        return out

    def get_bytes(self, key: str) -> bytes | None:
        "The content of key, or None if absent"
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _absent(exc):
                return None
            raise
        return obj['Body'].read()

    def put_bytes(self, key: str, data: bytes, sha256: str) -> None:
        "Store a small object whose sha256 the caller has computed"
        self._put(key, data, hashlib.md5(data).digest(), sha256)

    def copy(self, src: str, dst: str) -> None:
        """Server-side copy within the bucket, metadata included

        Only for objects under 5 GiB (single CopyObject); index files
        are far smaller.
        """
        with _wrapped(f'copy {src} -> {dst}'):
            self.s3.copy_object(
                Bucket=self.bucket,
                Key=dst,
                MetadataDirective='COPY',
                CopySource={'Bucket': self.bucket, 'Key': src},
            )

    def delete(self, key: str) -> None:
        "Delete one object; deleting an absent key is not an error"
        with _wrapped(f'delete {key}'):
            self.s3.delete_object(Bucket=self.bucket, Key=key)

    def delete_many(self, keys: list[str]) -> None:
        "Delete keys in batches of 1000; any per-key error is raised"
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            with _wrapped(f'delete_objects starting {batch[0]}'):
                resp = self.s3.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        'Objects': [{'Key': k} for k in batch],
                        'Quiet': True,
                    },
                )
            errors = resp.get('Errors', ())
            if errors:
                first = errors[0]
                raise RuntimeError(
                    f'delete failed for {len(errors)} keys, e.g. '
                    f'{first["Key"]}: {first.get("Message", "")}'
                )

    def put_file(
        self,
        key: str,
        path: Path,
        sha256: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Upload a local file whose sha256 the caller has verified

        Up to SINGLE_PUT_MAX it is one PUT with Content-MD5, and the
        returned ETag must be that MD5 (see _put). Larger files, which no
        package archive has, go multipart without that check.
        """
        path = Path(path)
        size = path.stat().st_size
        if size >= SINGLE_PUT_MAX:
            extra = {
                'Metadata': {'sha256': sha256},
                'ContentType': _content_type(key),
            }
            with _wrapped(f'put {key} ({size} bytes, multipart)'):
                self.s3.upload_file(
                    str(path),
                    self.bucket,
                    key,
                    Config=TRANSFER,
                    ExtraArgs=extra,
                    Callback=progress,
                )
            return
        with path.open('rb') as fh:
            md5 = _file_md5(fh)
            fh.seek(0)
            self._put(key, fh, md5, sha256)
        if progress:
            progress(size)

    def _put(self, key: str, body, md5: bytes, sha256: str) -> None:
        """One PUT, integrity checked at both ends

        Content-MD5 makes the server refuse a body that arrives
        corrupted; the returned ETag must then be that same MD5, or the
        server does not give us content MD5s (server side encryption, a
        gateway rewriting uploads) and the listing checks of verify would
        be meaningless.
        """
        with _wrapped(f'put {key}'):
            resp = self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentMD5=base64.b64encode(md5).decode(),
                Metadata={'sha256': sha256},
                ContentType=_content_type(key),
            )
        etag = resp.get('ETag', '').strip('"')
        if etag != md5.hex():
            raise IntegrityError(
                f'{key}: ETag {etag!r} after upload is '
                f'not the content MD5 {md5.hex()}'
            )


def _absent(exc: ClientError) -> bool:
    "Whether S3 said there is no such key"
    return exc.response['Error']['Code'] in ('404', 'NoSuchKey')


def _file_md5(fh) -> bytes:
    digest = hashlib.md5()
    while chunk := fh.read(1 << 20):
        digest.update(chunk)
    return digest.digest()


def _content_type(key: str) -> str:
    return CONTENT_TYPES.get(Path(key).suffix, 'application/octet-stream')
