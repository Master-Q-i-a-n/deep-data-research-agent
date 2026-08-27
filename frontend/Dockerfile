FROM node:22-alpine AS build

WORKDIR /app

# Install from the lock file before copying source so dependency layers remain
# reusable when only frontend code changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Production uses the same browser origin and lets Nginx forward /api to the
# backend container, so no public backend hostname is baked into the bundle.
ARG VITE_LANGGRAPH_API_URL=/api
ARG VITE_LANGGRAPH_ASSISTANT_ID=supervisor
ENV VITE_LANGGRAPH_API_URL=${VITE_LANGGRAPH_API_URL} \
    VITE_LANGGRAPH_ASSISTANT_ID=${VITE_LANGGRAPH_ASSISTANT_ID}

RUN npm run build


FROM nginx:stable-alpine AS runtime

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
