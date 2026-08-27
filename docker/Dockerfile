# Stage 1: build the frontend bundle (Node exists only at build time).
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Final stage: the Python services; the api serves the static bundle.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=frontend /fe/dist ./frontend/dist
CMD ["python", "-m", "zeitgeist.api.main"]
