from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage

from .crypto import decrypt_bytes, encrypt_bytes


class EncryptedFileSystemStorage(FileSystemStorage):
    """Every uploaded file is encrypted on disk and decrypted only when the app opens it."""

    def _save(self, name, content):
        content.seek(0)
        data = b"".join(content.chunks()) if hasattr(content, "chunks") else content.read()
        return super()._save(name, ContentFile(encrypt_bytes(data)))

    def _open(self, name, mode="rb"):
        with super()._open(name, "rb") as fh:
            raw = fh.read()
        return ContentFile(decrypt_bytes(raw), name=name)


class R2EncryptedStorage(Storage):
    """Cloudflare R2 (S3-compatible). Files are encrypted by the app BEFORE they leave the server, so the bucket only ever
    holds ciphertext. The bucket must stay private (no public access / no custom domain)."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            from django.conf import settings
            self._client = boto3.client(
                "s3", endpoint_url=settings.R2_ENDPOINT, aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY, region_name="auto",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}))
        return self._client

    @property
    def bucket(self):
        from django.conf import settings
        return settings.R2_BUCKET

    def get_available_name(self, name, max_length=None):
        return name  # names are random uuids already

    def _save(self, name, content):
        content.seek(0)
        data = b"".join(content.chunks()) if hasattr(content, "chunks") else content.read()
        self.client.put_object(Bucket=self.bucket, Key=name, Body=encrypt_bytes(data))
        return name

    def _open(self, name, mode="rb"):
        try:
            raw = self.client.get_object(Bucket=self.bucket, Key=name)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise FileNotFoundError(name)
        return ContentFile(decrypt_bytes(raw), name=name)

    def delete(self, name):
        if name:
            self.client.delete_object(Bucket=self.bucket, Key=name)

    def exists(self, name):
        try:
            self.client.head_object(Bucket=self.bucket, Key=name)
            return True
        except Exception:
            return False

    def size(self, name):
        return self.client.head_object(Bucket=self.bucket, Key=name)["ContentLength"]

    def url(self, name):
        raise NotImplementedError("files are served only through the login-protected download view")
