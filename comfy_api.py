import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests


COMFYUI_DIR = Path(os.environ.get("COMFYUI_DIR", "/opt/ComfyUI"))
COMFYUI_PORT = int(os.environ.get("COMFYUI_PORT", "8188"))
COMFYUI_URL = f"http://127.0.0.1:{COMFYUI_PORT}"
OUTPUT_DIR = Path(os.environ.get("COMFYUI_OUTPUT_DIR", "/data/outputs"))


class ComfyError(RuntimeError):
    pass


def start_comfy() -> subprocess.Popen:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        "python3",
        "main.py",
        "--listen",
        "127.0.0.1",
        "--port",
        str(COMFYUI_PORT),
        "--disable-auto-launch",
        "--output-directory",
        str(OUTPUT_DIR),
    ]
    return subprocess.Popen(
        command,
        cwd=str(COMFYUI_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_for_comfy(timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
            if response.ok:
                return
            last_error = response.text[:500]
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2)
    raise ComfyError(f"ComfyUI did not become ready: {last_error}")


def load_workflow(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        workflow = json.load(f)

    if "prompt" in workflow and isinstance(workflow["prompt"], dict):
        return workflow["prompt"]

    if all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
        return workflow

    raise ComfyError(
        "Workflow is not in ComfyUI API format. In ComfyUI, enable dev mode and "
        "export with 'Save (API Format)', then upload that JSON to the bucket."
    )


def patch_workflow(prompt: dict[str, Any], text: str, params: dict[str, Any]) -> dict[str, Any]:
    prompt = json.loads(json.dumps(prompt))
    positive_node = os.environ.get("PROMPT_NODE_ID", "").strip()

    if positive_node and positive_node in prompt:
        _patch_node_inputs(prompt[positive_node], text, params, force_text=True)
    else:
        patched_text = False
        for node in prompt.values():
            patched_text = _patch_node_inputs(node, text, params, force_text=not patched_text) or patched_text

    return prompt


def _patch_node_inputs(node: dict[str, Any], text: str, params: dict[str, Any], force_text: bool) -> bool:
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return False

    patched_text = False
    for key in ("text", "prompt", "positive_prompt", "caption"):
        if key in inputs and isinstance(inputs[key], str) and force_text:
            inputs[key] = text
            patched_text = True
            break

    replacements = {
        "width": params.get("width"),
        "height": params.get("height"),
        "num_frames": params.get("num_frames", params.get("frames")),
        "frames": params.get("num_frames", params.get("frames")),
        "seed": params.get("seed"),
        "steps": params.get("steps"),
        "cfg": params.get("cfg"),
        "cfg_scale": params.get("cfg", params.get("cfg_scale")),
    }
    for key, value in replacements.items():
        if value is not None and key in inputs and not isinstance(inputs[key], list):
            inputs[key] = value

    return patched_text


def queue_prompt(prompt: dict[str, Any], client_id: str) -> str:
    response = requests.post(
        f"{COMFYUI_URL}/prompt",
        json={"prompt": prompt, "client_id": client_id},
        timeout=60,
    )
    if not response.ok:
        raise ComfyError(f"ComfyUI rejected prompt: {response.status_code} {response.text[:2000]}")
    payload = response.json()
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        raise ComfyError(f"ComfyUI response did not include prompt_id: {payload}")
    return prompt_id


def wait_for_result(prompt_id: str, before: set[Path], timeout_seconds: int = 1800) -> Path:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        history = requests.get(f"{COMFYUI_URL}/history/{urllib.parse.quote(prompt_id)}", timeout=20)
        if history.ok and prompt_id in history.json():
            new_files = _new_output_files(before)
            if new_files:
                return new_files[-1]
            time.sleep(3)
            new_files = _new_output_files(before)
            if new_files:
                return new_files[-1]
            raise ComfyError("Prompt completed but no video/output file was found in the output directory")
        time.sleep(5)
    raise ComfyError("Timed out waiting for ComfyUI generation")


def current_outputs() -> set[Path]:
    if not OUTPUT_DIR.exists():
        return set()
    return {p for p in OUTPUT_DIR.rglob("*") if p.is_file()}


def _new_output_files(before: set[Path]) -> list[Path]:
    candidates = [p for p in current_outputs() if p not in before]
    preferred_ext = {".mp4", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
    candidates = [p for p in candidates if p.suffix.lower() in preferred_ext]
    return sorted(candidates, key=lambda p: p.stat().st_mtime)
