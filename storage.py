"""File storage that survives redeploys.

On a managed host the project directory is reset on every deploy, so anything
written to ``static/uploads`` or the private verification folder is lost. This
module keeps that same local behaviour for development, but switches to S3-compatible
object storage (Cloudflare R2, Supabase Storage, AWS S3, Backblaze B2) when the
required environment variables are set, so images persist across deploys.

Configuration (all optional - absent means "store on local disk"):

    S3_BUCKET            bucket name
    S3_ENDPOINT_URL      e.g. https://<accountid>.r2.cloudflarestorage.com
    S3_ACCESS_KEY_ID
    S3_SECRET_ACCESS_KEY
    S3_REGION            default 'auto' (fine for R2)
    S3_PUBLIC_BASE_URL   public domain/URL prefix used to build image URLs, e.g.
                         https://cdn.example.com  (R2 public bucket) or your worker URL

Two visibility levels are supported:

* ``public``   - listing photos, served to everyone.
* ``private``  - verification documents (NIN/CAC/face). Stored in a separate key
                 prefix and only ever read through the authenticated admin route,
                 never exposed as a public URL.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

_S3_CLIENT = None
_S3_ERROR = None


def _env(name: str, default: str = '') -> str:
    return (os.getenv(name) or default).strip()


def s3_bucket() -> str:
    return _env('S3_BUCKET')


def s3_enabled() -> bool:
    """True when object storage is configured; otherwise use the local disk."""
    return bool(s3_bucket() and _env('S3_ACCESS_KEY_ID') and _env('S3_SECRET_ACCESS_KEY'))


def public_base_url() -> str:
    return _env('S3_PUBLIC_BASE_URL').rstrip('/')


def _client():
    """Build (once) and return the boto3 S3 client."""
    global _S3_CLIENT, _S3_ERROR
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - depends on install
        _S3_ERROR = exc
        raise RuntimeError(
            'S3_BUCKET is set but boto3 is not installed. '
            'Add "boto3" to requirements.txt.'
        ) from exc
    kwargs = {
        'aws_access_key_id': _env('S3_ACCESS_KEY_ID'),
        'aws_secret_access_key': _env('S3_SECRET_ACCESS_KEY'),
        'region_name': _env('S3_REGION', 'auto') or 'auto',
        'config': Config(signature_version='s3v4', retries={'max_attempts': 2}),
    }
    endpoint = _env('S3_ENDPOINT_URL')
    if endpoint:
        kwargs['endpoint_url'] = endpoint
    _S3_CLIENT = boto3.client('s3', **kwargs)
    return _S3_CLIENT


def describe_backend() -> str:
    if not s3_enabled():
        return 'local disk (files are lost on redeploy)'
    return f'S3-compatible object storage (bucket={s3_bucket()})'


# --- Key helpers -----------------------------------------------------------

def _key(prefix: str, filename: str) -> str:
    prefix = prefix.strip('/')
    return f'{prefix}/{filename}' if prefix else filename


def public_key(filename: str) -> str:
    return _key(_env('S3_PUBLIC_PREFIX', 'uploads'), filename)


def private_key(filename: str) -> str:
    return _key(_env('S3_PRIVATE_PREFIX', 'verification_uploads'), filename)


# --- Writes / reads / deletes ---------------------------------------------

def save_bytes(filename: str, raw: bytes, visibility: str = 'public',
               content_type: Optional[str] = None) -> str:
    """Persist bytes and return the identifier to store in the database.

    When object storage is configured the returned value is the S3 key; otherwise
    it is just the filename, so existing local rows keep working unchanged.
    """
    if s3_enabled():
        key = public_key(filename) if visibility == 'public' else private_key(filename)
        extra = {'ContentType': content_type} if content_type else None
        if extra:
            _client().put_object(Bucket=s3_bucket(), Key=key, Body=raw, **extra)
        else:
            _client().put_object(Bucket=s3_bucket(), Key=key, Body=raw)
        return key
    return filename


def save_local_path(filename: str, raw: bytes, local_dir: Path) -> str:
    """Write bytes to the local directory (development / unconfigured fallback)."""
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / filename).write_bytes(raw)
    return filename


def read_bytes(identifier: str, local_dir: Path, visibility: str = 'public') -> Optional[bytes]:
    """Return the file's bytes, from object storage or the local disk."""
    if not identifier:
        return None
    if s3_enabled():
        key = identifier if '/' in identifier else (
            public_key(identifier) if visibility == 'public' else private_key(identifier)
        )
        try:
            obj = _client().get_object(Bucket=s3_bucket(), Key=key)
            return obj['Body'].read()
        except Exception:
            return None
    path = local_dir / identifier
    return path.read_bytes() if path.exists() else None


def delete(identifier: str, local_dir: Path, visibility: str = 'public') -> None:
    """Best-effort delete; never raises."""
    if not identifier:
        return
    if s3_enabled():
        key = identifier if '/' in identifier else (
            public_key(identifier) if visibility == 'public' else private_key(identifier)
        )
        try:
            _client().delete_object(Bucket=s3_bucket(), Key=key)
        except Exception:
            pass
        return
    try:
        (local_dir / identifier).unlink(missing_ok=True)
    except OSError:
        pass


def exists(identifier: str, local_dir: Path, visibility: str = 'public') -> bool:
    if not identifier:
        return False
    if s3_enabled():
        key = identifier if '/' in identifier else (
            public_key(identifier) if visibility == 'public' else private_key(identifier)
        )
        try:
            _client().head_object(Bucket=s3_bucket(), Key=key)
            return True
        except Exception:
            return False
    return (local_dir / identifier).exists()


def public_url(identifier: str, local_url: Optional[str]) -> Optional[str]:
    """Build the browser-facing URL for a stored file.

    ``local_url`` is the value produced by ``url_for('static', ...)`` and is used
    when object storage is not configured, or when the identifier is a legacy
    local filename that was never migrated.
    """
    if not identifier:
        return None
    if not s3_enabled():
        return local_url
    if '/' in identifier and _env('S3_PUBLIC_PREFIX', 'uploads') + '/' not in identifier:
        # Already a full key (possibly private); only public keys get a public URL.
        key = identifier
    else:
        key = identifier if '/' in identifier else public_key(identifier)
    base = public_base_url()
    if not base:
        # Fall back to a signed URL when no public domain is configured.
        try:
            return _client().generate_presigned_url(
                'get_object', Params={'Bucket': s3_bucket(), 'Key': key}, ExpiresIn=3600)
        except Exception:
            return local_url
    return f'{base}/{key}'


def migrate_local_file(filename: str, local_dir: Path, visibility: str = 'public') -> Optional[str]:
    """Upload an existing local file to object storage and remove the local copy.

    Used by the one-off migration helper so listings created before object storage
    was configured keep working.
    """
    if not s3_enabled() or not filename:
        return None
    path = local_dir / filename
    if not path.exists():
        return None
    raw = path.read_bytes()
    key = save_bytes(filename, raw, visibility=visibility)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return key
