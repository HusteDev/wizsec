# _cache.py
import json
import os
import shutil
import sys
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from .config import Config, DEFAULT_TEMP_FOLDER
from .exceptions import WizCacheError


def safe_write_json(
    data: Any,
    filename: str,
    save_path: Path,
    temp_path: Union[str, Path] = DEFAULT_TEMP_FOLDER,
) -> None:
    """Write data as JSON atomically via a temp file, then move into place."""
    logger = Config.get_logger()
    original = Path(filename)
    temp_filename = f"{original.stem}_temp{original.suffix}"
    temp_file = Path(temp_path) / temp_filename

    def serialize_object(obj: Any) -> Any:
        try:
            return vars(obj) if hasattr(obj, "__dict__") else str(obj)
        except TypeError:
            return str(obj)

    try:
        with open(temp_file, "w+") as file:
            json.dump(data, file, indent=2, default=serialize_object)
        shutil.move(temp_file, save_path / filename)
    except Exception as e:
        _, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.extract_tb(exc_traceback)
        last_call = tb[-1]
        logger.error("Safe Write Error - at line %s", last_call.lineno)
        raise
    finally:
        temp_file = Path(temp_file)
        if temp_file.exists():
            temp_file.unlink()


def find_file_with_extension(filepath: str, extension_order: List[str]) -> str:
    """Find the first existing file matching filepath with one of the given extensions."""
    path_dir, file_name = os.path.split(filepath)
    filename, extension = os.path.splitext(file_name)

    if extension:
        full_path = os.path.join(path_dir, f"{filename}{extension}")
        if os.path.exists(full_path):
            return full_path
    else:
        for ext in extension_order:
            full_path = os.path.join(path_dir, f"{filename}{ext}")
            if os.path.exists(full_path):
                return full_path
    return ""


class CacheBackend(ABC):
    """Abstract base for interval-based cache backends."""

    @abstractmethod
    def get(self, key: str) -> Tuple[Any, Optional[datetime]]:
        """Return (data, stored_timestamp). data is None on cache miss."""
        ...

    @abstractmethod
    def set(self, key: str, data: Any, ttl_seconds: Optional[int] = None) -> None:
        """Persist data under key. ttl_seconds is advisory for S3/filesystem; used by DynamoDB."""
        ...


class FilesystemBackend(CacheBackend):
    """Local-file backend. No-ops on set() in serverless environments."""

    def __init__(self, directory: Optional[str] = None, extension_order=None):
        from .config import DEFAULT_WIZ_DIR

        self._dir = Path(directory) if directory else DEFAULT_WIZ_DIR / ".cache"
        self._ext_order = extension_order or [".pkl", ".json", ".csv"]

    def get(self, key: str) -> Tuple[Any, Optional[datetime]]:
        from .utils import load_file

        base = str(self._dir / key) if self._dir else key
        full_path = find_file_with_extension(base, self._ext_order)
        if not full_path or os.path.getsize(full_path) <= 1000:
            return None, None
        ts = datetime.fromtimestamp(os.path.getmtime(full_path), tz=timezone.utc)
        return load_file(full_path), ts

    def set(self, key: str, data: Any, ttl_seconds: Optional[int] = None) -> None:
        if Config.serverless():
            Config.get_logger().debug(
                "FilesystemBackend.set skipped in serverless mode."
            )
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        from .utils import dump_to_json

        dump_to_json(data, filename=key, filepath=str(self._dir))


def _make_boto3_session(
    region: Optional[str] = None,
    profile: Optional[str] = None,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    aws_session_token: Optional[str] = None,
    role_arn: Optional[str] = None,
    role_session_name: str = "wizsec-cache",
):
    """Build a boto3 Session from the supplied credentials, then optionally assume a role.

    Resolution order (first wins):
      1. Explicit key/secret/token params
      2. Named profile from ~/.aws/credentials
      3. boto3 default credential chain (env vars, instance profile, etc.)

    If role_arn is provided the session calls STS AssumeRole and returns a new
    session using the temporary credentials from that call.
    """
    try:
        import boto3
    except ImportError:
        raise WizCacheError(
            "boto3 is required for AWS cache backends. pip install boto3"
        )

    session_kwargs: dict = {"region_name": region}
    if profile:
        session_kwargs["profile_name"] = profile
    if aws_access_key_id:
        session_kwargs["aws_access_key_id"] = aws_access_key_id
        session_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            session_kwargs["aws_session_token"] = aws_session_token

    session = boto3.Session(**session_kwargs)

    if role_arn:
        try:
            sts = session.client("sts")
            assumed = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=role_session_name,
            )["Credentials"]
            session = boto3.Session(
                region_name=region,
                aws_access_key_id=assumed["AccessKeyId"],
                aws_secret_access_key=assumed["SecretAccessKey"],
                aws_session_token=assumed["SessionToken"],
            )
        except Exception as e:
            raise WizCacheError(f"Failed to assume role '{role_arn}'", e)

    return session


class DynamoDBBackend(CacheBackend):
    """DynamoDB backend. Stores JSON + created_at + TTL per cache key.

    Table schema:
      PK: cache_key (S)
      Attrs: data (S, JSON), created_at (S, ISO-8601), ttl (N, epoch seconds)
    """

    def __init__(
        self,
        table_name: str,
        ttl_seconds: int = 86400,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        role_arn: Optional[str] = None,
        role_session_name: str = "wizsec-cache",
    ):
        self._table_name = table_name
        self._ttl_seconds = ttl_seconds
        self._session = _make_boto3_session(
            region=region or Config.get("aws", "region", default=None),
            profile=profile,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            role_arn=role_arn,
            role_session_name=role_session_name,
        )

    def _table(self):
        dynamodb = self._session.resource("dynamodb")
        return dynamodb.Table(self._table_name)

    def get(self, key: str) -> Tuple[Any, Optional[datetime]]:
        try:
            response = self._table().get_item(Key={"cache_key": key})
        except Exception as e:
            raise WizCacheError(f"DynamoDB get failed for key '{key}'", e)

        item = response.get("Item")
        if not item:
            return None, None

        try:
            data = json.loads(item["data"])
            ts = datetime.fromisoformat(item["created_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return data, ts
        except Exception as e:
            raise WizCacheError(f"DynamoDB item parse failed for key '{key}'", e)

    def set(self, key: str, data: Any, ttl_seconds: Optional[int] = None) -> None:
        import time

        effective_ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        now = datetime.now(timezone.utc)
        try:
            self._table().put_item(
                Item={
                    "cache_key": key,
                    "data": json.dumps(data, default=str),
                    "created_at": now.isoformat(),
                    "ttl": int(time.time()) + effective_ttl,
                }
            )
        except Exception as e:
            raise WizCacheError(f"DynamoDB set failed for key '{key}'", e)


class S3Backend(CacheBackend):
    """S3 backend. Uses LastModified as the cache timestamp (no extra metadata needed).

    Object key pattern: {prefix}/{cache_key}.json
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "wizsec-cache",
        region: Optional[str] = None,
        profile: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        role_arn: Optional[str] = None,
        role_session_name: str = "wizsec-cache",
    ):
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._session = _make_boto3_session(
            region=region or Config.get("aws", "region", default=None),
            profile=profile,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            role_arn=role_arn,
            role_session_name=role_session_name,
        )

    def _client(self):
        return self._session.client("s3")

    def _object_key(self, key: str) -> str:
        name = key if key.endswith(".json") else f"{key}.json"
        return f"{self._prefix}/{name}"

    def get(self, key: str) -> Tuple[Any, Optional[datetime]]:
        from botocore.exceptions import ClientError

        s3 = self._client()
        obj_key = self._object_key(key)

        try:
            head = s3.head_object(Bucket=self._bucket, Key=obj_key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None, None
            raise WizCacheError(f"S3 head_object failed for '{obj_key}'", e)
        except Exception as e:
            raise WizCacheError(f"S3 head_object failed for '{obj_key}'", e)

        ts: datetime = head["LastModified"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        try:
            body = s3.get_object(Bucket=self._bucket, Key=obj_key)["Body"].read()
            data = json.loads(body)
        except Exception as e:
            raise WizCacheError(f"S3 get_object failed for '{obj_key}'", e)

        return data, ts

    def set(self, key: str, data: Any, ttl_seconds: Optional[int] = None) -> None:
        s3 = self._client()
        obj_key = self._object_key(key)
        try:
            s3.put_object(
                Bucket=self._bucket,
                Key=obj_key,
                Body=json.dumps(data, default=str).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as e:
            raise WizCacheError(f"S3 put_object failed for '{obj_key}'", e)


def get_configured_backend() -> CacheBackend:
    """Instantiate the backend from config. Falls back to FilesystemBackend."""
    backend_name = Config.get("cache", "backend", default="filesystem")

    # Shared AWS credential params — read once, passed to whichever backend needs them
    aws_kwargs = dict(
        region=Config.get("cache", "aws_region")
        or Config.get("aws", "region", default=None),
        profile=Config.get("cache", "aws_profile", default=None),
        aws_access_key_id=Config.get("cache", "aws_access_key_id", default=None),
        aws_secret_access_key=Config.get(
            "cache", "aws_secret_access_key", default=None
        ),
        aws_session_token=Config.get("cache", "aws_session_token", default=None),
        role_arn=Config.get("cache", "aws_role_arn", default=None),
        role_session_name=Config.get(
            "cache", "aws_role_session_name", default="wizsec-cache"
        ),
    )

    if backend_name == "dynamodb":
        table = Config.get("cache", "dynamodb_table")
        if not table:
            raise WizCacheError(
                "cache.dynamodb_table must be set when backend=dynamodb"
            )
        ttl = Config.get("cache", "ttl_seconds", default=2592000)
        return DynamoDBBackend(table_name=table, ttl_seconds=int(ttl), **aws_kwargs)

    if backend_name == "s3":
        bucket = Config.get("cache", "s3_bucket")
        if not bucket:
            raise WizCacheError("cache.s3_bucket must be set when backend=s3")
        prefix = Config.get("cache", "s3_prefix", default="wizsec-cache")
        return S3Backend(bucket=bucket, prefix=prefix, **aws_kwargs)

    return FilesystemBackend()


def _build_cache_key(query: str, vars: Optional[dict] = None) -> str:
    """Build a stable, human-readable cache key from a GraphQL query + its variables.

    Strips pagination-only vars (after, first) before hashing so all pages of the
    same logical query share one cache entry.
    """
    import hashlib
    from graphql import parse as gql_parse
    from graphql.language import print_ast
    from .utils import parse_query_metadata

    try:
        normalized = print_ast(gql_parse(query))
    except Exception:
        normalized = query

    filtered_vars = {
        k: v for k, v in (vars or {}).items() if k not in ("after", "first")
    }
    raw = normalized + json.dumps(filtered_vars, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]

    try:
        source = parse_query_metadata(query).get("source", "query")
    except Exception:
        source = "query"

    return f"{source}_{digest}"
