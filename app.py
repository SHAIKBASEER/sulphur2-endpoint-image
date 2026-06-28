import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bucket_cli import BucketError, cp_from_bucket, cp_to_bucket, download_file_from_repo, download_from_repo
from comfy_api import (
    COMFYUI_DIR,
    DEFAULT_FPS,
    ComfyError,
    build_negative_prompt,
    build_positive_prompt,
    comfy_log_tail,
    current_outputs,
    load_workflow,
    patch_workflow,
    queue_prompt,
    recent_output_files,
    start_comfy,
    wait_for_comfy,
    wait_for_result,
)


BUCKET_ID = os.environ.get("BUCKET_ID", "lucifershaik/Sulphur-2-base-bucket")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "outputs").strip("/")
CHECKPOINT_KEY = os.environ.get("CHECKPOINT_KEY", "sulphur_dev_bf16.safetensors")
CHECKPOINT_NAME = os.environ.get("CHECKPOINT_NAME", Path(CHECKPOINT_KEY).name)
LORA_KEY = os.environ.get(
    "LORA_KEY",
    "distill_loras/ltx-2.3-22b-distilled-lora-1.1_fro90_ceil72_condsafe.safetensors",
)
LORA_NAME = os.environ.get("LORA_NAME", Path(LORA_KEY).name)
WORKFLOW_KEY = os.environ.get("WORKFLOW_KEY", "workflows/ltx23_t2v_api.json")
TEXT_ENCODER_REPO = os.environ.get(
    "TEXT_ENCODER_REPO", "Comfy-Org/ltx-2"
)
TEXT_ENCODER_KEY = os.environ.get(
    "TEXT_ENCODER_KEY",
    "split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors",
)
TEXT_ENCODER_NAME = os.environ.get(
    "TEXT_ENCODER_NAME",
    "gemma_3_12B_it_fp4_mixed.safetensors",
)
UPSCALER_REPO = os.environ.get("UPSCALER_REPO", "Lightricks/LTX-2.3")
UPSCALER_KEY = os.environ.get(
    "UPSCALER_KEY", "ltx-2.3-spatial-upscaler-x2-1.0.safetensors"
)
UPSCALER_NAME = os.environ.get("UPSCALER_NAME", Path(UPSCALER_KEY).name)
MODEL_DOWNLOAD_TIMEOUT = int(os.environ.get("MODEL_DOWNLOAD_TIMEOUT", "7200"))
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT", "2400"))
STARTUP_DOWNLOADS = os.environ.get("STARTUP_DOWNLOADS", "1") == "1"

DATA_DIR = Path(os.environ.get("WORKDIR", "/data"))
WORKFLOW_PATH = DATA_DIR / "workflows" / Path(WORKFLOW_KEY).name
CHECKPOINT_PATH = COMFYUI_DIR / "models" / "checkpoints" / CHECKPOINT_NAME
LORA_PATH = COMFYUI_DIR / "models" / "loras" / LORA_NAME
TEXT_ENCODER_DIR = COMFYUI_DIR / "models" / "text_encoders"
TEXT_ENCODER_PATH = TEXT_ENCODER_DIR / TEXT_ENCODER_NAME
UPSCALER_PATH = COMFYUI_DIR / "models" / "latent_upscale_models" / UPSCALER_NAME


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


@app.get("/health")
def health_check() -> dict[str, Any]:
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
        "text_encoder_path": str(TEXT_ENCODER_PATH),
        "upscaler_path": str(UPSCALER_PATH),
        "workflow_exists": WORKFLOW_PATH.exists(),
        "checkpoint_exists": CHECKPOINT_PATH.exists(),
        "lora_exists": LORA_PATH.exists(),
        "text_encoder_exists": TEXT_ENCODER_PATH.exists(),
        "text_encoder_size_bytes": TEXT_ENCODER_PATH.stat().st_size if TEXT_ENCODER_PATH.exists() else 0,
        "upscaler_exists": UPSCALER_PATH.exists(),
        "comfy_process_running": _comfy_process is not None and _comfy_process.poll() is None,
        "comfy_process_returncode": None if _comfy_process is None else _comfy_process.poll(),
        "recent_outputs": recent_output_files(),
        "comfy_log_tail": comfy_log_tail(),
    }


@app.post("/")
def generate_root(request: GenerateRequest) -> dict[str, Any]:
    return generate(request)


@app.post("/debug/workflow")
def debug_workflow(request: GenerateRequest) -> dict[str, Any]:
    try:
        prepare_runtime()
        workflow = patch_workflow(load_workflow(WORKFLOW_PATH), request.inputs, request.parameters)
        class_counts: dict[str, int] = {}
        output_like_nodes = []
        latent_debug_nodes = []
        prompt_debug_nodes = []
        for node_id, node in workflow.items():
            class_type = str(node.get("class_type", ""))
            class_counts[class_type] = class_counts.get(class_type, 0) + 1
            if class_type in {"VHS_VideoCombine", "SaveVideo", "SaveImage", "PreviewImage"}:
                output_like_nodes.append(
                    {
                        "node_id": node_id,
                        "class_type": class_type,
                        "inputs": node.get("inputs", {}),
                    }
                )
            if class_type in {"EmptyLTXVLatentVideo", "LTXVEmptyLatentAudio"}:
                latent_debug_nodes.append(
                    {
                        "node_id": node_id,
                        "class_type": class_type,
                        "inputs": node.get("inputs", {}),
                    }
                )
            if class_type in {"PrimitiveString", "PrimitiveStringMultiline", "CLIPTextEncode"}:
                text_inputs = {
                    key: value
                    for key, value in (node.get("inputs", {}) or {}).items()
                    if key in {"value", "text", "prompt", "negative_prompt", "caption", "string"}
                    and isinstance(value, str)
                }
                if text_inputs:
                    prompt_debug_nodes.append(
                        {
                            "node_id": node_id,
                            "class_type": class_type,
                            "inputs": text_inputs,
                        }
                    )
        return {
            "status": "ok",
            "node_count": len(workflow),
            "has_vhs_output": any(node["class_type"] == "VHS_VideoCombine" for node in output_like_nodes),
            "output_like_nodes": output_like_nodes,
            "latent_debug_nodes": latent_debug_nodes,
            "prompt_debug_nodes": prompt_debug_nodes,
            "effective_positive_prompt": build_positive_prompt(request.inputs, request.parameters),
            "effective_negative_prompt": build_negative_prompt(request.parameters),
            "quality_preset": request.parameters.get("quality_preset", request.parameters.get("preset", "cinematic_ultra")),
            "effective_fps": request.parameters.get("fps", request.parameters.get("frame_rate", DEFAULT_FPS)),
            "shape_override_enabled": bool(request.parameters.get("override_shape", request.parameters.get("allow_shape_override", False))),
            "class_counts": class_counts,
        }
    except (BucketError, ComfyError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Unexpected failure: {exc}") from exc


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
            "note": "For quality, shape overrides are ignored unless parameters.override_shape is true.",
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
    if TEXT_ENCODER_REPO and not TEXT_ENCODER_PATH.exists():
        if TEXT_ENCODER_KEY:
            download_file_from_repo(
                TEXT_ENCODER_REPO,
                TEXT_ENCODER_KEY,
                TEXT_ENCODER_PATH,
                timeout=MODEL_DOWNLOAD_TIMEOUT,
            )
        else:
            target_dir = TEXT_ENCODER_DIR / Path(TEXT_ENCODER_NAME).parts[0]
            download_from_repo(TEXT_ENCODER_REPO, target_dir, filename=None, timeout=MODEL_DOWNLOAD_TIMEOUT)
    if UPSCALER_REPO and not UPSCALER_PATH.exists():
        download_from_repo(
            UPSCALER_REPO,
            UPSCALER_PATH.parent,
            filename=UPSCALER_KEY,
            timeout=MODEL_DOWNLOAD_TIMEOUT,
        )
