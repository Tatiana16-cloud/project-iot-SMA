FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catalog.py .
COPY device_templates.json .
# catalog.json and device_pool.json are mounted as volumes in /data (mutable state)
# device_templates.json is shipped with the image (read-only config)
ENV PORT=9080 \
    CATALOG_PATH=/data/catalog.json \
    CATALOG_TEMPLATES_PATH=/app/device_templates.json \
    CATALOG_DEVICE_POOL_PATH=/data/device_pool.json
CMD ["python","catalog.py"]
