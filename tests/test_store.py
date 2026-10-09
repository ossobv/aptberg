import base64
import hashlib
import io
from datetime import datetime, timezone

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ParamValidationError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from aptberg.store import TRANSFER, IntegrityError, Store, StoreError


def _store(monkeypatch):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    store = Store('bkt', endpoint='http://s3.invalid', region='us-east-1')
    return store, Stubber(store.s3)


def test_list_sizes_paginates(monkeypatch):
    "Every page of the listing is read"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'list_objects_v2',
        {
            'Contents': [{'Key': '_pool/a', 'Size': 1}],
            'IsTruncated': True,
            'NextContinuationToken': 't',
        },
        {'Bucket': 'bkt', 'Prefix': '_pool/'},
    )
    stub.add_response(
        'list_objects_v2',
        {'Contents': [{'Key': '_pool/b', 'Size': 2}], 'IsTruncated': False},
        {'Bucket': 'bkt', 'Prefix': '_pool/', 'ContinuationToken': 't'},
    )
    with stub:
        assert store.list_sizes('_pool/') == {'_pool/a': 1, '_pool/b': 2}


def test_sha256(monkeypatch):
    "Metadata digest, empty without our metadata, None when absent"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'head_object',
        {'Metadata': {'sha256': 'ab'}},
        {'Bucket': 'bkt', 'Key': 'k'},
    )
    stub.add_response(
        'head_object', {'Metadata': {}}, {'Bucket': 'bkt', 'Key': 'k'}
    )
    stub.add_client_error('head_object', '404', http_status_code=404)
    with stub:
        assert store.sha256('k') == 'ab'
        assert store.sha256('k') == ''
        assert store.sha256('k') is None


def test_get_bytes(monkeypatch):
    "The body; None for a missing key; other errors are not hidden"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'get_object',
        {'Body': StreamingBody(io.BytesIO(b'data'), 4)},
        {'Bucket': 'bkt', 'Key': 'k'},
    )
    stub.add_client_error('get_object', 'NoSuchKey', http_status_code=404)
    stub.add_client_error('get_object', 'AccessDenied', http_status_code=403)
    with stub:
        assert store.get_bytes('k') == b'data'
        assert store.get_bytes('k') is None
        with pytest.raises(ClientError, match='AccessDenied'):
            store.get_bytes('k')


def test_sha256_error_not_hidden(monkeypatch):
    "Only a 404 means absent; a 403 is not taken for a missing object"
    store, stub = _store(monkeypatch)
    stub.add_client_error('head_object', '403', http_status_code=403)
    with stub, pytest.raises(ClientError):
        store.sha256('k')


def test_list_dirs(monkeypatch):
    "The names one level down, across pages"
    store, stub = _store(monkeypatch)
    want = {'Bucket': 'bkt', 'Prefix': 'u/ch/acc/dists/', 'Delimiter': '/'}
    stub.add_response(
        'list_objects_v2',
        {
            'CommonPrefixes': [{'Prefix': 'u/ch/acc/dists/jammy/'}],
            'IsTruncated': True,
            'NextContinuationToken': 't',
        },
        want,
    )
    stub.add_response(
        'list_objects_v2',
        {
            'CommonPrefixes': [{'Prefix': 'u/ch/acc/dists/noble/'}],
            'IsTruncated': False,
        },
        {**want, 'ContinuationToken': 't'},
    )
    with stub:
        assert store.list_dirs('u/ch/acc/dists/') == ['jammy', 'noble']


def test_put_file(monkeypatch, tmp_path):
    "The sha256 and a deb content type ride along with the upload"
    store, stub = _store(monkeypatch)
    path = tmp_path / 'x.deb'
    path.write_bytes(b'deb')
    md5 = hashlib.md5(b'deb')
    stub.add_response(
        'put_object',
        {'ETag': f'"{md5.hexdigest()}"'},
        {
            'Bucket': 'bkt',
            'Key': '_pool/x.deb',
            'Body': ANY,
            'ContentMD5': base64.b64encode(md5.digest()).decode(),
            'Metadata': {'sha256': 'ab'},
            'ContentType': 'application/vnd.debian.binary-package',
        },
    )
    with stub:
        store.put_file('_pool/x.deb', path, 'ab')
    stub.assert_no_pending_responses()


def test_put_etag_not_md5(monkeypatch):
    "An ETag that is not the content MD5 is an integrity error"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'put_object',
        {'ETag': '"0123-1"'},
        {
            'Bucket': 'bkt',
            'Key': 'k',
            'Body': b'x',
            'ContentMD5': ANY,
            'Metadata': {'sha256': 'ab'},
            'ContentType': 'application/octet-stream',
        },
    )
    with stub, pytest.raises(IntegrityError, match='not the content MD5'):
        store.put_bytes('k', b'x', 'ab')


def test_list_objects(monkeypatch):
    "Sizes and modification times"
    store, stub = _store(monkeypatch)
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    stub.add_response(
        'list_objects_v2',
        {
            'Contents': [{'Key': '_pool/a', 'Size': 1, 'LastModified': when}],
            'IsTruncated': False,
        },
        {'Bucket': 'bkt', 'Prefix': '_pool/'},
    )
    with stub:
        assert store.list_objects('_pool/') == {'_pool/a': (1, when)}


def test_delete_many_batches_and_errors(monkeypatch):
    "1000 keys per request; a per-key error is raised"
    store, stub = _store(monkeypatch)
    keys = [f'k{i:04}' for i in range(1500)]
    stub.add_response(
        'delete_objects',
        {},
        {
            'Bucket': 'bkt',
            'Delete': {
                'Objects': [{'Key': k} for k in keys[:1000]],
                'Quiet': True,
            },
        },
    )
    stub.add_response(
        'delete_objects',
        {
            'Errors': [
                {'Key': 'k1400', 'Code': 'AccessDenied', 'Message': 'no'}
            ]
        },
        {
            'Bucket': 'bkt',
            'Delete': {
                'Objects': [{'Key': k} for k in keys[1000:]],
                'Quiet': True,
            },
        },
    )
    with stub, pytest.raises(RuntimeError, match='k1400: no'):
        store.delete_many(keys)
    stub.assert_no_pending_responses()


def test_copy_error_names_the_keys(monkeypatch):
    "A CopyObject failure is a StoreError naming src and dst, not raw"
    store, stub = _store(monkeypatch)
    stub.add_client_error('copy_object', 'AccessDenied', http_status_code=403)
    with stub, pytest.raises(StoreError, match='copy a -> b.*AccessDenied'):
        store.copy('a', 'b')
    stub.assert_no_pending_responses()


def test_delete_error_names_the_key(monkeypatch):
    "A DeleteObject failure is a StoreError naming the key, not raw"
    store, stub = _store(monkeypatch)
    stub.add_client_error(
        'delete_object', 'AccessDenied', http_status_code=403
    )
    with stub, pytest.raises(StoreError, match='delete k.*AccessDenied'):
        store.delete('k')
    stub.assert_no_pending_responses()


def test_put_error_names_the_key(monkeypatch):
    "A PutObject failure is a StoreError naming the key, not raw"
    store, stub = _store(monkeypatch)
    stub.add_client_error('put_object', 'AccessDenied', http_status_code=403)
    with stub, pytest.raises(StoreError, match='put k.*AccessDenied'):
        store.put_bytes('k', b'x', 'ab')
    stub.assert_no_pending_responses()


def test_tenant_bucket(monkeypatch):
    "RGW's tenant:bucket is accepted, path-style, colon encoded"
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    store = Store(
        'tenant:aptberg', endpoint='https://rgw.invalid', region='us-east-1'
    )
    seen = []

    class Raw:
        def stream(self, **kwargs):
            return iter([b''])

    def send(request, **kwargs):
        seen.append((request.url, request.headers.get('x-amz-copy-source')))
        return AWSResponse(request.url, 200, {}, Raw())

    store.s3.meta.events.register('before-send.s3', send)
    store.copy('_snap/x/InRelease', 'ubuntu/ch/acc/dists/x/InRelease')
    assert seen == [
        (
            'https://rgw.invalid/tenant%3Aaptberg/ubuntu/ch/acc/dists/x/InRelease',
            b'tenant%3Aaptberg/_snap/x/InRelease',
        )
    ]


def test_bad_bucket_names(monkeypatch):
    "Anything else botocore would refuse is still refused"
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    for bad in ('a:b:c', 'has space', ':bucket', 'tenant:'):
        store = Store(bad, endpoint='https://rgw.invalid', region='us-east-1')
        with pytest.raises(ParamValidationError):
            store.sha256('k')


def test_single_put_up_to_5gib(monkeypatch, tmp_path):
    "A 20 MiB file is one PutObject, so its ETag is its MD5"
    assert TRANSFER.multipart_threshold == 5 << 30
    store, stub = _store(monkeypatch)
    path = tmp_path / 'big.deb'
    path.write_bytes(b'\0' * (20 << 20))
    etag = hashlib.md5(path.read_bytes()).hexdigest()
    stub.add_response(
        'put_object',
        {'ETag': f'"{etag}"'},
        {
            'Bucket': 'bkt',
            'Key': '_pool/big.deb',
            'Body': ANY,
            'ContentMD5': ANY,
            'Metadata': {'sha256': 'ab'},
            'ContentType': 'application/vnd.debian.binary-package',
        },
    )
    with stub:
        store.put_file('_pool/big.deb', path, 'ab')
    stub.assert_no_pending_responses()


def test_multipart_above_single_put_max(monkeypatch, tmp_path):
    "Past the single PUT limit: multipart, still with sha256 and type"
    monkeypatch.setattr('aptberg.store.SINGLE_PUT_MAX', 10)
    store, _ = _store(monkeypatch)
    calls = []
    monkeypatch.setattr(
        store.s3, 'upload_file', lambda *a, **kw: calls.append((a, kw))
    )
    path = tmp_path / 'big.deb'
    path.write_bytes(b'\0' * 11)
    store.put_file('_pool/big.deb', path, 'ab')
    ((args, kw),) = calls
    assert args == (str(path), 'bkt', '_pool/big.deb')
    assert kw['Config'] is TRANSFER
    assert kw['ExtraArgs'] == {
        'Metadata': {'sha256': 'ab'},
        'ContentType': 'application/vnd.debian.binary-package',
    }


def test_check_ok(monkeypatch):
    "A working bucket passes with one cheap call"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'list_objects_v2',
        {'Contents': [], 'IsTruncated': False},
        {'Bucket': 'bkt', 'MaxKeys': 1},
    )
    with stub:
        store.check()
    stub.assert_no_pending_responses()


def test_check_bad_credentials(monkeypatch):
    "A credentials or permission error is a clear StoreError, not raw"
    store, stub = _store(monkeypatch)
    stub.add_client_error(
        'list_objects_v2', 'AccessDenied', http_status_code=403
    )
    with stub, pytest.raises(StoreError, match='bkt.*AccessDenied'):
        store.check()


def test_list_etags(monkeypatch):
    "Sizes and unquoted ETags"
    store, stub = _store(monkeypatch)
    stub.add_response(
        'list_objects_v2',
        {
            'Contents': [
                {'Key': '_pool/a', 'Size': 1, 'ETag': '"abc"'},
                {'Key': '_pool/b', 'Size': 2, 'ETag': '"def-3"'},
            ],
            'IsTruncated': False,
        },
        {'Bucket': 'bkt', 'Prefix': '_pool/'},
    )
    with stub:
        assert store.list_etags('_pool/') == {
            '_pool/a': (1, 'abc'),
            '_pool/b': (2, 'def-3'),
        }
