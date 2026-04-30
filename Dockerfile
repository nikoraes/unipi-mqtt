FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY unipi_mqtt.py ./unipi_mqtt.py
COPY config.yaml .

# Allow overriding config via a mounted volume
VOLUME ["/app/config.yaml"]

CMD ["python", "-u", "unipi_mqtt.py"]
