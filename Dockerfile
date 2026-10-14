# Noba: the fraud-agent tools server, checkout and bank console.
#   docker compose up -d --build
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The server code. .env, audit/ and .elevenlabs_state.json are mounted by
# docker-compose.yml so secrets and call recordings never live in the image.
COPY *.py ./

EXPOSE 8080
CMD ["uvicorn", "el_tools:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
