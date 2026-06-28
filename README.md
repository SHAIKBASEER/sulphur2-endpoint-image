# Sulphur-2 Hugging Face Endpoint Custom Container

This is a custom-container starter for serving `SulphurAI/Sulphur-2-base` through
Hugging Face Inference Endpoints.

It does **not** use the HF Default Engine. The Default Engine already failed on
bucket writes and cannot run Sulphur/LTXVideo directly.

## What This Container Does

1. Starts a FastAPI server on port `5000`.
2. Downloads configured files from your HF Bucket:
   - one checkpoint
   - one LoRA
   - one ComfyUI API-format workflow JSON
3. Starts ComfyUI with the Lightricks `ComfyUI-LTXVideo` custom nodes.
4. Queues the workflow through the ComfyUI HTTP API.
5. Uploads the generated output file back to your bucket.
6. Adds a `VHS_VideoCombine` MP4 output fallback when native `SaveVideo`
   completes without a ComfyUI history output.

## Required Bucket Files

Your bucket is:

```text
hf://buckets/lucifershaik/Sulphur-2-base-bucket
```

Recommended keys:

```text
sulphur_dev_fp8mixed.safetensors
distill_loras/ltx-2.3-22b-distilled-lora-1.1_fro90_ceil72_condsafe.safetensors
workflows/ltx23_t2v_api.json
```

Important: `ltx23_t2v_api.json` must be a **ComfyUI API-format workflow**.
In ComfyUI, enable developer mode and export with **Save (API Format)**.
The regular UI workflow JSON may not work with the API.

## Build And Push

### Option A: GitHub Actions, Recommended

Create a GitHub repo named:

```text
sulphur2-endpoint-image
```

Upload all files in this folder, including:

```text
.github/workflows/build-ghcr.yml
```

After you push to `main`, GitHub Actions will build and push:

```text
ghcr.io/lucifershaik/sulphur2-endpoint:latest
```

After the first successful build, open the package in GitHub and set its
visibility to **Public** so Hugging Face Endpoints can pull it without registry
credentials.

### Option B: Local Docker

From this folder:

```powershell
docker login
.\build_push.ps1 -Image "YOUR_DOCKERHUB_USER/sulphur2-endpoint" -Tag "latest"
```

Example:

```powershell
.\build_push.ps1 -Image "lucifershaik/sulphur2-endpoint" -Tag "latest"
```

## Hugging Face Endpoint Settings

Create a new Endpoint from:

```text
Model repo: lucifershaik/sulphur-2-endpoint
Inference Engine: Custom Container
Container image: YOUR_DOCKERHUB_USER/sulphur2-endpoint:latest
Hardware: A100 80GB first
```

Use A100 80GB first. T4/16GB cannot run Sulphur-2 generation.

Default env:

```text
BUCKET_ID=lucifershaik/Sulphur-2-base-bucket
OUTPUT_PREFIX=outputs
CHECKPOINT_KEY=sulphur_dev_fp8mixed.safetensors
CHECKPOINT_NAME=sulphur_dev_fp8mixed.safetensors
LORA_KEY=distill_loras/ltx-2.3-22b-distilled-lora-1.1_fro90_ceil72_condsafe.safetensors
LORA_NAME=ltx-2.3-22b-distilled-lora-1.1_fro90_ceil72_condsafe.safetensors
WORKFLOW_KEY=workflows/ltx23_t2v_api.json
TEXT_ENCODER_REPO=Comfy-Org/ltx-2
TEXT_ENCODER_KEY=split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
TEXT_ENCODER_NAME=gemma_3_12B_it_fp4_mixed.safetensors
STARTUP_DOWNLOADS=1
MODEL_DOWNLOAD_TIMEOUT=7200
GENERATION_TIMEOUT=2400
DEFAULT_FPS=24
OUTPUT_VIDEO_FORMAT=video/h264-mp4
```

Secret env:

```text
HF_TOKEN=<new Hugging Face token>
```

## Test Request

```powershell
$env:HF_TOKEN="YOUR_TOKEN"

$body = Get-Content .\request-example.json -Raw

Invoke-RestMethod `
  -Uri "https://YOUR-ENDPOINT.eu-west-1.aws.endpoints.huggingface.cloud" `
  -Method Post `
  -Headers @{
    Authorization = "Bearer $env:HF_TOKEN"
    Accept = "application/json"
  } `
  -ContentType "application/json" `
  -Body $body
```

Before running a long generation, verify the converted workflow contains the
fallback video output:

```powershell
Invoke-RestMethod `
  -Uri "https://YOUR-ENDPOINT.us-east-1.aws.endpoints.huggingface.cloud/debug/workflow" `
  -Method Post `
  -Headers @{
    Authorization = "Bearer $env:HF_TOKEN"
    Accept = "application/json"
  } `
  -ContentType "application/json" `
  -Body $body
```

Expected debug field:

```json
{
  "has_vhs_output": true
}
```

Expected response:

```json
{
  "status": "ok",
  "job_id": "...",
  "prompt_id": "...",
  "video": "hf://buckets/lucifershaik/Sulphur-2-base-bucket/outputs/.../output.mp4"
}
```

## If Startup Fails

Open:

```text
https://YOUR-ENDPOINT.../status
```

The API keeps running even if startup preparation fails, so `/status` should show
which file/key is missing or which download failed.

## Notes

- This container downloads only the configured checkpoint, not the entire 187GB
  bucket/repo.
- Use the FP8 checkpoint first to keep disk and VRAM requirements lower.
- Revoke any token pasted into chat or terminal screenshots.
