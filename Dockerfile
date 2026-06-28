FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PORT=5000
ENV COMFYUI_DIR=/opt/ComfyUI
ENV COMFYUI_PORT=8188
ENV WORKDIR=/data
ENV HF_HOME=/data/hf-cache
ENV HF_HUB_ENABLE_HF_TRANSFER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3 \
    python3-dev \
    python3-venv \
    python3-pip \
    git \
    git-lfs \
    ffmpeg \
    curl \
    ca-certificates \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Main runtime dependencies for the endpoint API.
COPY requirements-api.txt /tmp/requirements-api.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements-api.txt

# Install PyTorch CUDA before ComfyUI requirements so ComfyUI does not pull CPU torch.
RUN python3 -m pip install --no-cache-dir \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# ComfyUI and LTXVideo custom nodes.
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git ${COMFYUI_DIR}
WORKDIR ${COMFYUI_DIR}
RUN python3 -m pip install --no-cache-dir -r requirements.txt

RUN mkdir -p ${COMFYUI_DIR}/custom_nodes \
    && git clone --depth 1 https://github.com/Lightricks/ComfyUI-LTXVideo.git ${COMFYUI_DIR}/custom_nodes/ComfyUI-LTXVideo \
    && if [ -f ${COMFYUI_DIR}/custom_nodes/ComfyUI-LTXVideo/requirements.txt ]; then \
         python3 -m pip install --no-cache-dir -r ${COMFYUI_DIR}/custom_nodes/ComfyUI-LTXVideo/requirements.txt; \
       fi

RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git ${COMFYUI_DIR}/custom_nodes/ComfyUI-VideoHelperSuite \
    && python3 -m pip install --no-cache-dir -r ${COMFYUI_DIR}/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt

# Isolated latest HF CLI for Bucket cp/upload commands. This avoids conflicts with
# model libraries that may need older huggingface_hub APIs.
RUN python3 -m venv /opt/hfcli \
    && /opt/hfcli/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/hfcli/bin/pip install --no-cache-dir "huggingface-hub[cli]>=1.0.0" hf_transfer

WORKDIR /app
COPY app.py bucket_cli.py comfy_api.py /app/

RUN mkdir -p /data/models/checkpoints /data/models/loras /data/workflows /data/outputs \
    && mkdir -p ${COMFYUI_DIR}/models/checkpoints ${COMFYUI_DIR}/models/loras

EXPOSE 5000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-5000} --timeout-keep-alive 300"]
