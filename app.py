import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bucket_cli import BucketError, cp_from_bucket, cp_to_bucket
from comfy_api import (
    COMFYUI_DIR,
    ComfyError,
    current_outputs,
    load_workflow,
    patch_workflow,
    queue_prompt,
    start_comfy,
    wait_for_comfy,
    wait_for_result,
)


BUCKET_ID = os.environ.get("BUCKET_ID", "lucifershaik/Sulphur-2-base-bucket")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "outputs").strip("/")
CHECKPOINT_KEY = os.environ.get("CHECKPOINT_KEY", "sulphur_dev_fp8mixed.safetensors")
CHECKPOINT_NAME = os.environ.get("CHECKPOINT_NAME", Path(CHECKPOINT_KEY).name)
LORA_KEY = os.environ.get(
    "LORA_KEY",
    "distill_loras/ltx-2.3-22b-distilled-lora-1.1_fro90_ceil72_condsafe.safetensors",
)
LORA_NAME = os.environ.get("LORA_NAME", Path(LORA_KEY).name)
WORKFLOW_KEY = os.environ.get("WORKFLOW_KEY", "workflows/ltx23_t2v_api.json")
MODEL_DOWNLOAD_TIMEOUT = int(os.environ.get("MODEL_DOWNLOAD_TIMEOUT", "7200"))
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT", "2400"))
STARTUP_DOWNLOADS = os.environ.get("STARTUP_DOWNLOADS", "1") == "1"

DATA_DIR = Path(os.environ.get("WORKDIR", "/data"))
WORKFLOW_PATH = DATA_DIR / "workflows" / Path(WORKFLOW_KEY).name
CHECKPOINT_PATH = COMFYUI_DIR / "models" / "checkpoints" / CHECKPOINT_NAME
LORA_PATH = COMFYUI_DIR / "models" / "loras" / LORA_NAME


class GenerateRequest(BaseModel):
    inputs: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None


app = FastAPI(title="Sulphur-2 Custom Container")
_comfy_process = None
_startup_error: str | None = None
_startup_lock = threading.Lock()


@app.on_event("startup")
def startup() -> None:
    global _startup_error
    try:
        prepare_runtime()
    except Exception as exc:  # noqa: BLE001
        _startup_error = str(exc)
        # Keep the API alive so /health and /status explain the failure.


@app.get("/")
def health() -> dict[str, Any]:
    return {"status": "ok", "startup_error": _startup_error}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        "status": "ready" if _startup_error is None else "startup_error",
        "startup_error": _startup_error,
        "bucket_id": BUCKET_ID,
        "checkpoint_path": str(CHECKPOINT_PATH),
        "lora_path": str(LORA_PATH),
        "workflow_path": str(WORKFLOW_PATH),
        "workflow_exists": WORKFLOW_PATH.exists(),
        "checkpoint_exists": CHECKPOINT_PATH.exists(),
        "lora_exists": LORA_PATH.exists(),
    }


@app.post("/")
def generate_root(request: GenerateRequest) -> dict[str, Any]:
    return generate(request)


@app.post("/generate")
def generate(request: GenerateRequest) -> dict[str, Any]:
    try:
        prepare_runtime()
        job_id = request.job_id or uuid.uuid4().hex
        workflow = patch_workflow(load_workflow(WORKFLOW_PATH), request.inputs, request.parameters)
        before = current_outputs()
        prompt_id = queue_prompt(workflow, client_id=job_id)
        output_file = wait_for_result(prompt_id, before=before, timeout_seconds=GENERATION_TIMEOUT)
        remote_key = f"{OUTPUT_PREFIX}/{job_id}/{output_file.name}"
        remote_uri = cp_to_bucket(output_file, BUCKET_ID, remote_key, timeout=MODEL_DOWNLOAD_TIMEOUT)
        return {
            "status": "ok",
            "job_id": job_id,
            "prompt_id": prompt_id,
            "video": remote_uri,
        }
    except (BucketError, ComfyError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Unexpected failure: {exc}") from exc


def prepare_runtime() -> None:
    global _comfy_process, _startup_error
    with _startup_lock:
        if _startup_error:
            _startup_error = None

        if STARTUP_DOWNLOADS:
            download_assets()

        if _comfy_process is None or _comfy_process.poll() is not None:
            _comfy_process = start_comfy()
            time.sleep(2)
            wait_for_comfy()


def download_assets() -> None:
    if not CHECKPOINT_PATH.exists():
        cp_from_bucket(BUCKET_ID, CHECKPOINT_KEY, CHECKPOINT_PATH, timeout=MODEL_DOWNLOAD_TIMEOUT)
    if LORA_KEY and not LORA_PATH.exists():
        cp_from_bucket(BUCKET_ID, LORA_KEY, LORA_PATH, timeout=MODEL_DOWNLOAD_TIMEOUT)
    if not WORKFLOW_PATH.exists():
        cp_from_bucket(BUCKET_ID, WORKFLOW_KEY, WORKFLOW_PATH, timeout=300)
