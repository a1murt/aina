# Next.js standalone image for the Qost Twin web UI (port 3000, GET /healthz).
FROM node:22-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm install --global pnpm@12.4.1 && npm cache clean --force
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS build
# /api and /ws are proxied to the api service (same origin for the browser); Next resolves the
# rewrite destinations at build time.
ARG API_URL=http://api:8000
ENV API_URL=${API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:22-alpine AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/public ./public
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
USER app
EXPOSE 3000
CMD ["node", "server.js"]
