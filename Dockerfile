FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# CPU-only torch keeps the image ~2 GB smaller than the default CUDA build.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
# ultralytics pulls in the GUI opencv; swap it for the headless build (no libGL needed).
RUN pip install -r requirements.txt \
 && pip uninstall -y opencv-python opencv-python-headless \
 && pip install opencv-python-headless
COPY . .
RUN useradd -u 1000 -m app && mkdir /config /weights && chown app /config
USER app
ENV ANALYTICS_CONFIG=/config/cameras.json WEB_PORT=5000
EXPOSE 5000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
