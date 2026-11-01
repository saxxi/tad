# tado local in a container: Docker Desktop already has macOS Local Network access, which a
# host Python under launchd doesn't get (Local Network privacy → "No route to host").
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
