# Stage 1: Build dashboard
FROM node:20-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ .
RUN npm run build

# Stage 1b: Build control-ui (only its admin console ships on the hosted server)
FROM node:20-slim AS controlui
WORKDIR /control-ui
COPY control-ui/package.json control-ui/package-lock.json ./
RUN npm ci
COPY control-ui/ .
RUN npm run build:fast

# Stage 2: Python server
FROM python:3.12-slim

WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
COPY --from=dashboard /dashboard/dist ./static/
# Admin Console served at /admin/. Copy only the admin entry + its content-hashed
# asset chunks — NOT the companion's dist/index.html, which would shadow the
# dashboard's own index.html at /.
COPY --from=controlui /control-ui/dist/admin ./static/admin/
COPY --from=controlui /control-ui/dist/assets ./static/assets/

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
CMD ["/app/entrypoint.sh"]
