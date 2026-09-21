FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn pydantic httpx

# Copy repository content
COPY . .

# Hugging Face Spaces defaults to port 7860
EXPOSE 7860

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7860"]
