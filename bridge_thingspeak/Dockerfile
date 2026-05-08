FROM python:3.11-slim

WORKDIR /app

# Reqs del bridge
COPY bridge_thingspeak/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código compartido y del servicio
COPY common ./common
COPY bridge_thingspeak/bridge.py .
COPY bridge_thingspeak/settings.json .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "bridge.py"]



