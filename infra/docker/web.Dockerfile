# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim AS dev

ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=development

WORKDIR /app

COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json

RUN npm ci

COPY apps/web ./apps/web
COPY packages ./packages

RUN mkdir -p /app/apps/web/.next \
    && chown -R node:node /app

USER node

EXPOSE 3000

CMD ["npm", "run", "dev", "--workspace", "@zara/web", "--", "--hostname", "0.0.0.0"]
