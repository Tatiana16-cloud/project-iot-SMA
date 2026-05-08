FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catalog.py .
COPY device_templates.json .
# montaremos el JSON como volumen en /data
ENV PORT=9080 CATALOG_PATH=/data/catalog.json CATALOG_TEMPLATES_PATH=/app/device_templates.json
CMD ["python","catalog.py"]
