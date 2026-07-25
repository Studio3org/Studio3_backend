"""boto3 S3 client factory."""

import os


def _env(name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def get_s3_client():
    import boto3

    access_key = _env("AWS_ACCESS_KEY_ID")
    secret_key = _env("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise RuntimeError(
            "S3 is partially configured: set AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY (and S3_BUCKET) on the host environment."
        )

    return boto3.client(
        "s3",
        region_name=_env("AWS_REGION") or "us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def get_bucket() -> str:
    return _env("S3_BUCKET", "STUDIO3_S3_BUCKET")


def get_public_base_url() -> str:
    explicit = _env("S3_PUBLIC_BASE_URL", "STUDIO3_S3_PUBLIC_BASE_URL")
    if explicit:
        return explicit
    bucket = get_bucket()
    return f"https://{bucket}.s3.amazonaws.com" if bucket else ""


def s3_configured() -> bool:
    """True only when bucket + credentials are all present.

    A bucket alone is not enough — boto3 raises NoCredentialsError when
    signing, which surfaced as a generic 500 on Render when env vars were
    only partially set.
    """
    return bool(
        get_bucket()
        and _env("AWS_ACCESS_KEY_ID")
        and _env("AWS_SECRET_ACCESS_KEY")
    )