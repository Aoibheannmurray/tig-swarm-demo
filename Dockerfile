# Stage 1: Build dashboard
FROM node:20-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ .
RUN npm run build

# Stage 1b: Build control-ui (its admin console + join page ship on the hosted server)
FROM node:20-slim AS controlui
WORKDIR /control-ui
COPY control-ui/package.json control-ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY control-ui/ .
RUN npm run build:fast

# Stage 2: Python server
FROM python:3.12-slim

WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY server/ .
COPY --from=dashboard /dashboard/dist ./static/
# Admin Console served at /admin/, contributor join page at /join/. Copy only
# those entries + the content-hashed asset chunks — NOT the companion's
# dist/index.html, which would shadow the dashboard's own index.html at /.
COPY --from=controlui /control-ui/dist/admin ./static/admin/
COPY --from=controlui /control-ui/dist/join ./static/join/
COPY --from=controlui /control-ui/dist/assets ./static/assets/
# Public assets the admin/join pages reference at root (icon, fonts) + the
# join page's PWA manifest & service worker. Copied individually so the
# companion's dist/index.html never lands at / (it would shadow the dashboard).
COPY --from=controlui /control-ui/dist/prometheus-icon.png /control-ui/dist/prometheus.png /control-ui/dist/fonts.css /control-ui/dist/manifest.webmanifest /control-ui/dist/sw.js ./static/
COPY --from=controlui /control-ui/dist/fonts ./static/fonts/

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
CMD ["/app/entrypoint.sh"]
