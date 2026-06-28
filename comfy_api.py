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
        if class_type == "SaveVideo" and "VHS_VideoCombine" in object_info:
            # Native SaveVideo has completed without history outputs on some
            # ComfyUI builds. A VHS output node is injected after conversion.
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

    if "VHS_VideoCombine" in object_info:
        _add_vhs_video_output_node(prompt, workflow, object_info, raw_link_map, nodes_by_id, resolve_link_origin)

    if not prompt:
        raise ComfyError("Converted UI workflow is empty")
    return prompt


def _add_vhs_video_output_node(
    prompt: dict[str, Any],
    workflow: dict[str, Any],
    object_info: dict[str, Any],
    raw_link_map: dict[int, dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
    resolve_link_origin,
) -> None:
    if any(node.get("class_type") == "VHS_VideoCombine" for node in prompt.values()):
        return

    images_ref, filename_prefix = _find_video_images_reference(
        workflow,
        raw_link_map,
        nodes_by_id,
        prompt,
        resolve_link_origin,
    )
    if images_ref is None:
        return

    node_id = _next_prompt_node_id(prompt)
    input_names = set(object_input_names(object_info, "VHS_VideoCombine"))
    inputs: dict[str, Any] = {"images": images_ref}
    defaults = {
        "frame_rate": int(os.environ.get("DEFAULT_FPS", "24")),
        "loop_count": 0,
        "filename_prefix": filename_prefix or "video/LTX_2.3_t2v",
        "format": os.environ.get("OUTPUT_VIDEO_FORMAT", "video/h264-mp4"),
        "pingpong": False,
        "save_output": True,
    }
    for name, value in defaults.items():
        if name in input_names:
            inputs[name] = value

    prompt[node_id] = {
        "class_type": "VHS_VideoCombine",
        "inputs": inputs,
    }


def _find_video_images_reference(
    workflow: dict[str, Any],
    raw_link_map: dict[int, dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
    prompt: dict[str, Any],
    resolve_link_origin,
) -> tuple[list[Any] | None, str | None]:
    for node in workflow.get("nodes") or []:
        if node.get("type") != "SaveVideo":
            continue
        prefix = _filename_prefix_from_widgets(node)
        for node_input in node.get("inputs") or []:
            if node_input.get("name") != "video" or node_input.get("link") is None:
                continue
            images_ref = _create_video_images_from_video_link(
                int(node_input["link"]),
                raw_link_map,
                nodes_by_id,
                prompt,
                resolve_link_origin,
            )
            if images_ref is not None:
                return images_ref, prefix

    for node_id, node in prompt.items():
        if node.get("class_type") == "CreateVideo":
            inputs = node.get("inputs", {})
            images_ref = inputs.get("images")
            if isinstance(images_ref, list):
                return images_ref, "video/LTX_2.3_t2v"

    return None, None


def _create_video_images_from_video_link(
    link_id: int,
    raw_link_map: dict[int, dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
    prompt: dict[str, Any],
    resolve_link_origin,
) -> list[Any] | None:
    link = raw_link_map.get(link_id)
    if not link:
        return None

    create_video_id = str(link.get("origin_id"))
    create_video_prompt_node = prompt.get(create_video_id, {})
    if create_video_prompt_node.get("class_type") == "CreateVideo":
        images_ref = create_video_prompt_node.get("inputs", {}).get("images")
        if isinstance(images_ref, list):
            return images_ref

    create_video_ui_node = nodes_by_id.get(create_video_id, {})
    if create_video_ui_node.get("type") != "CreateVideo":
        return None

    for create_input in create_video_ui_node.get("inputs") or []:
        if create_input.get("name") == "images" and create_input.get("link") is not None:
            return resolve_link_origin(int(create_input["link"]))
    return None


def _filename_prefix_from_widgets(node: dict[str, Any]) -> str | None:
    widgets = node.get("widgets_values")
    if isinstance(widgets, dict):
        value = widgets.get("filename_prefix") or widgets.get("filename")
        return str(value) if value else None
    if isinstance(widgets, list) and widgets:
        return str(widgets[0])
    return None


def _next_prompt_node_id(prompt: dict[str, Any]) -> str:
    numeric_ids = []
    for node_id in prompt:
        try:
            numeric_ids.append(int(node_id))
        except ValueError:
            continue
    return str((max(numeric_ids) if numeric_ids else 0) + 1000)


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

    # Patch common generation settings only when the workflow node has that input.
    # NOTE: FPS and bit_depth are different settings. A CreateVideo node accepts
    # bit_depth values only in the 8-10 range; 24 belongs in fps/frame_rate.
    fps_value = params.get("fps", params.get("frame_rate"))
    bit_depth_value = params.get("bit_depth")

    replacements = {
        "width": params.get("width"),
        "height": params.get("height"),
        "num_frames": params.get("num_frames", params.get("frames")),
        "frames": params.get("num_frames", params.get("frames")),
        "seed": params.get("seed"),
        "steps": params.get("steps"),
        "cfg": params.get("cfg"),
        "cfg_scale": params.get("cfg", params.get("cfg_scale")),
        "fps": fps_value,
        "frame_rate": fps_value,
        "bit_depth": bit_depth_value,
    }
    for key, value in replacements.items():
        if value is not None and key in inputs and not isinstance(inputs[key], list):
            inputs[key] = value

    # Safety fix for workflows exported with fps=24 accidentally stored as
    # bit_depth=24. ComfyUI CreateVideo validation rejects bit_depth > 10.
    if "bit_depth" in inputs and not isinstance(inputs["bit_depth"], list):
        try:
            current_bit_depth = int(inputs["bit_depth"])
        except (TypeError, ValueError):
            current_bit_depth = 8

        if current_bit_depth not in (8, 10):
            inputs["bit_depth"] = 8

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


PREFERRED_OUTPUT_EXT = {".mp4", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".mov", ".mkv", ".avi"}


def wait_for_result(prompt_id: str, before: set[Path], timeout_seconds: int = 1800) -> Path:
    # Use a timestamp too, because some ComfyUI workflows overwrite the same
    # output filename. In that case the path is already in `before`, but the
    # file mtime changes after this request starts.
    started_at = time.time()
    deadline = started_at + timeout_seconds
    last_history_payload: dict[str, Any] | None = None

    while time.time() < deadline:
        history = requests.get(f"{COMFYUI_URL}/history/{urllib.parse.quote(prompt_id)}", timeout=20)
        if history.ok:
            payload = history.json()
            if prompt_id in payload:
                last_history_payload = payload

                # Give ComfyUI a few seconds to finish flushing video files.
                for _ in range(6):
                    history_files = _output_files_from_history(payload, prompt_id)
                    if history_files:
                        return history_files[-1]

                    new_files = _new_output_files(before, since_time=started_at - 5)
                    if new_files:
                        return new_files[-1]

                    time.sleep(3)
                    refreshed = requests.get(
                        f"{COMFYUI_URL}/history/{urllib.parse.quote(prompt_id)}",
                        timeout=20,
                    )
                    if refreshed.ok:
                        payload = refreshed.json()
                        last_history_payload = payload

                summary = _history_output_summary(last_history_payload, prompt_id)
                raise ComfyError(
                    "Prompt completed, but no video/output file could be resolved. "
                    f"Checked OUTPUT_DIR={OUTPUT_DIR} and TEMP_DIR={COMFYUI_DIR / 'temp'}. "
                    f"ComfyUI history outputs summary: {summary}. "
                    "This usually means the workflow has no final Save/CreateVideo output node, "
                    "the output node is set to preview/temp only, or the output extension is unsupported."
                )
        time.sleep(5)
    raise ComfyError("Timed out waiting for ComfyUI generation")


def current_outputs() -> set[Path]:
    if not OUTPUT_DIR.exists():
        return set()
    return {p for p in OUTPUT_DIR.rglob("*") if p.is_file()}


def recent_output_files(limit: int = 20) -> list[str]:
    files: list[Path] = []
    for root in (OUTPUT_DIR, COMFYUI_DIR / "temp"):
        if root.exists():
            files.extend(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in PREFERRED_OUTPUT_EXT)
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in files[:limit]]


def _new_output_files(before: set[Path], since_time: float | None = None) -> list[Path]:
    candidates = []
    for p in current_outputs():
        if p.suffix.lower() not in PREFERRED_OUTPUT_EXT:
            continue
        try:
            modified_after_start = since_time is not None and p.stat().st_mtime >= since_time
        except FileNotFoundError:
            continue
        if p not in before or modified_after_start:
            candidates.append(p)
    return sorted(candidates, key=lambda p: p.stat().st_mtime)


def _output_files_from_history(history_payload: dict[str, Any], prompt_id: str) -> list[Path]:
    entry = history_payload.get(prompt_id, {})
    outputs = entry.get("outputs", {})
    found: list[Path] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "filename" in value and isinstance(value["filename"], str):
                path = _comfy_file_to_path(value)
                if path and path.exists() and path.suffix.lower() in PREFERRED_OUTPUT_EXT:
                    found.append(path)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(outputs)
    return sorted(set(found), key=lambda p: p.stat().st_mtime)


def _comfy_file_to_path(file_info: dict[str, Any]) -> Path | None:
    filename = file_info.get("filename")
    if not filename:
        return None

    subfolder = str(file_info.get("subfolder") or "").strip("/")
    file_type = str(file_info.get("type") or "output").lower()

    if file_type == "temp":
        base_dir = COMFYUI_DIR / "temp"
    elif file_type == "input":
        base_dir = COMFYUI_DIR / "input"
    else:
        base_dir = OUTPUT_DIR

    return base_dir / subfolder / filename if subfolder else base_dir / filename


def _history_output_summary(history_payload: dict[str, Any] | None, prompt_id: str) -> dict[str, Any]:
    if not history_payload or prompt_id not in history_payload:
        return {"prompt_id_found": False}

    outputs = history_payload.get(prompt_id, {}).get("outputs", {})
    filenames: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "filename" in value:
                filenames.append(str(value.get("filename")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(outputs)
    return {
        "prompt_id_found": True,
        "output_node_ids": list(outputs.keys()) if isinstance(outputs, dict) else [],
        "filenames": filenames[:20],
    }
