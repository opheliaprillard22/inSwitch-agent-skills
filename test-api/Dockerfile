FROM python:3.11-slim

WORKDIR /app
COPY app.py services.json instances.json /app/

EXPOSE 8001
CMD ["python", "/app/app.py"]
