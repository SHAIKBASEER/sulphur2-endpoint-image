import os
import subprocess
from pathlib import Path


HF_BIN = os.environ.get("HF_BIN", "/opt/hfcli/bin/hf")


class BucketError(RuntimeError):
    pass


def bucket_uri(bucket_id: str, key: str) -> str:
    clean_key = key.strip("/")
    return f"hf://buckets/{bucket_id}/{clean_key}"


def run_hf(args: list[str], timeout: int | None = None) -> str:
    env = os.environ.copy()
    token = env.get("HF_TOKEN")
    if not token:
        raise BucketError("HF_TOKEN is required for bucket access")

    result = subprocess.run(
        [HF_BIN, *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise BucketError(f"hf {' '.join(args)} failed: {message}")
    return result.stdout.strip()


def cp_from_bucket(bucket_id: str, key: str, local_path: Path, timeout: int | None = None) -> Path:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    run_hf(["buckets", "cp", bucket_uri(bucket_id, key), str(local_path)], timeout=timeout)
    return local_path


def cp_to_bucket(local_path: Path, bucket_id: str, key: str, timeout: int | None = None) -> str:
    if not local_path.exists():
        raise BucketError(f"Local file does not exist: {local_path}")

    remote_uri = bucket_uri(bucket_id, key)
    run_hf(["buckets", "cp", str(local_path), remote_uri], timeout=timeout)
    return remote_uri


def download_from_repo(
    repo_id: str,
    local_dir: Path,
    filename: str | None = None,
    timeout: int | None = None,
) -> Path:
    local_dir.mkdir(parents=True, exist_ok=True)
    args = ["download", repo_id]
    if filename:
        args.append(filename)
    args.extend(["--local-dir", str(local_dir)])
    run_hf(args, timeout=timeout)
    return local_dir
