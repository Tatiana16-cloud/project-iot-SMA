FROM python:3.10-slim

WORKDIR /app

COPY reportGenerator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY reportGenerator/reporting_service.py .
COPY reportGenerator/settings.json .

EXPOSE 8093
CMD ["python3", "reporting_service.py"]
