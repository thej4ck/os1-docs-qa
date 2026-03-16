FROM python:3.13-slim

WORKDIR /app

# Dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Ensure directories exist
RUN mkdir -p data searchdata static

# search.db and help-files/ should be prepared before build:
#   ./scripts/prepare_deploy.sh
# search.db is baked into the image (regenerated when docs change)
# help-files/ contains HTML help + images (baked into image)
# data/app.db lives on a persistent volume (NOT in image)

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
