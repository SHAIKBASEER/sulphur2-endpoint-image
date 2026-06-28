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
COMFY_LOG_PATH = Path(os.environ.get("COMFY_LOG_PATH", "/data/comfyui.log"))


class ComfyError(RuntimeError):
    pass


def start_comfy() -> subprocess.Popen:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    COMFY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = COMFY_LOG_PATH.open("a", encoding="utf-8")
    log_file.write("\n\n--- starting ComfyUI ---\n")
    log_file.flush()
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
        stdout=log_file,
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


def comfy_log_tail(max_chars: int = 8000) -> str:
    if not COMFY_LOG_PATH.exists():
        return ""
    data = COMFY_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    return data[-max_chars:]


def load_workflow(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        workflow = json.load(f)

    if "prompt" in workflow and isinstance(workflow["prompt"], dict):
        return workflow["prompt"]

    if all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
        return workflow

    if "nodes" in workflow and "links" in workflow:
        return convert_ui_workflow_to_api(workflow)

    raise ComfyError("Workflow is neither ComfyUI API format nor ComfyUI UI workflow format")


def convert_ui_workflow_to_api(workflow: dict[str, Any]) -> dict[str, Any]:
    object_info = get_object_info()
    links = workflow.get("links") or []
    raw_link_map: dict[int, dict[str, Any]] = {}
    for link in links:
        if isinstance(link, list) and len(link) >= 6:
            link_id, origin_id, origin_slot = link[0], link[1], link[2]
            raw_link_map[int(link_id)] = {
                "origin_id": origin_id,
                "origin_slot": origin_slot,
                "target_id": link[3],
                "target_slot": link[4],
                "type": link[5],
            }

    nodes_by_id = {str(node.get("id")): node for node in workflow.get("nodes") or []}

    def resolve_link_origin(link_id: int, seen: set[int] | None = None) -> list[Any]:
        seen = seen or set()
        if link_id in seen:
            raise ComfyError(f"Reroute cycle detected while resolving link {link_id}")
        seen.add(link_id)

        link = raw_link_map[int(link_id)]
        origin_id = str(link["origin_id"])
        origin_slot = int(link["origin_slot"])
        origin_node = nodes_by_id.get(origin_id, {})
        origin_type = origin_node.get("type")

        if origin_type == "Reroute" or origin_type not in object_info:
            for node_input in origin_node.get("inputs") or []:
                upstream_link = node_input.get("link")
                if upstream_link is not None:
                    return resolve_link_origin(int(upstream_link), seen)
            raise ComfyError(f"Skipped node {origin_id} ({origin_type}) has no upstream input")

        return [origin_id, origin_slot]

    prompt: dict[str, Any] = {}
    for node in workflow.get("nodes") or []:
        node_id = str(node.get("id"))
        class_type = node.get("type")
        if not node_id or not class_type:
            continue
        if class_type in {"Reroute", "Note"}:
            continue
        if class_type not in object_info:
            continue

        inputs: dict[str, Any] = {}
        linked_names: set[str] = set()
        for node_input in node.get("inputs") or []:
            link_id = node_input.get("link")
            name = node_input.get("name")
            if link_id is not None and name and int(link_id) in raw_link_map:
                inputs[name] = resolve_link_origin(int(link_id))
                linked_names.add(name)

        widget_values = list(node.get("widgets_values") or [])
        widget_index = 0
        for input_name in object_input_names(object_info, class_type):
            if input_name in linked_names or input_name in inputs:
                continue
            if widget_index < len(widget_values):
                inputs[input_name] = widget_values[widget_index]
                widget_index += 1

        prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
        }

    if not prompt:
        raise ComfyError("Converted UI workflow is empty")
    return prompt


def get_object_info() -> dict[str, Any]:
    response = requests.get(f"{COMFYUI_URL}/object_info", timeout=60)
    if not response.ok:
        raise ComfyError(f"Could not fetch ComfyUI object_info: {response.status_code} {response.text[:1000]}")
    return response.json()


def object_input_names(object_info: dict[str, Any], class_type: str) -> list[str]:
    info = object_info.get(class_type, {})
    input_info = info.get("input", {})
    names: list[str] = []
    for group_name in ("required", "optional"):
        group = input_info.get(group_name, {})
        if isinstance(group, dict):
            names.extend(group.keys())
    return names


def patch_workflow(prompt: dict[str, Any], text: str, params: dict[str, Any]) -> dict[str, Any]:
    prompt = json.loads(json.dumps(prompt))
    positive_node = os.environ.get("PROMPT_NODE_ID", "").strip()
    checkpoint_name = os.environ.get("CHECKPOINT_NAME", "").strip()
    lora_name = os.environ.get("LORA_NAME", "").strip()
    text_encoder_name = os.environ.get("TEXT_ENCODER_NAME", "").strip()
    upscaler_name = os.environ.get("UPSCALER_NAME", "").strip()

    if positive_node and positive_node in prompt:
        _patch_node_inputs(prompt[positive_node], text, params, force_text=True)
    else:
        patched_text = False
        for node in prompt.values():
            patched_text = _patch_node_inputs(node, text, params, force_text=not patched_text) or patched_text

    for node in prompt.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in ("ckpt_name", "checkpoint_name"):
            if checkpoint_name and key in inputs and isinstance(inputs[key], str):
                inputs[key] = checkpoint_name
        for key in ("ltxv_path", "model_path"):
            if checkpoint_name and key in inputs and isinstance(inputs[key], str):
                inputs[key] = checkpoint_name
        for key in ("text_encoder", "gemma_path"):
            if text_encoder_name and key in inputs and isinstance(inputs[key], str):
                inputs[key] = text_encoder_name
        if upscaler_name and node.get("class_type") == "LatentUpscaleModelLoader":
            if "model_name" in inputs and isinstance(inputs["model_name"], str):
                inputs["model_name"] = upscaler_name
        for key in ("lora_name", "loras"):
            if lora_name and key in inputs and isinstance(inputs[key], str):
                inputs[key] = lora_name

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
