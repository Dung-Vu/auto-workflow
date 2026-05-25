FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data directory (mount as Docker volume for token persistence)
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 5050

CMD ["python", "app.py"]
