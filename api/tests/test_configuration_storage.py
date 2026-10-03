"""Internal storage adapters retain the values used to construct them."""
from app.config import use_configuration
from app.config_values import ConfigurationValues
from app.storage import get_storage, reset_storage


def test_s3_operation_keeps_its_bucket_prefix_and_key_after_another_selection(monkeypatch, tmp_path):
    import boto3
    uploaded = []
    class Client:
        def upload_fileobj(self, source, bucket, key, ExtraArgs):
            uploaded.append((source.read(), bucket, key, ExtraArgs))
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs: Client())
    document = tmp_path / 'synthetic.pdf'
    document.write_bytes(b'synthetic')
    first = ConfigurationValues.from_environment({'STORAGE_BACKEND': 's3', 'S3_BUCKET': 'original',
        'S3_PREFIX': 'original/', 'S3_KMS_KEY_ID': 'original-key'})
    second = first.with_patch({'s3_bucket': 'changed', 's3_prefix': 'changed/', 's3_kms_key_id': ''})
    reset_storage()
    try:
        with use_configuration(first):
            original = get_storage()
        with use_configuration(second):
            changed = get_storage()
            assert changed is not original
            assert original.put_pdf(str(document), 'fax.pdf') == 's3://original/original/fax.pdf'
        assert uploaded == [(b'synthetic', 'original', 'original/fax.pdf',
                             {'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': 'original-key'})]
    finally:
        reset_storage()
