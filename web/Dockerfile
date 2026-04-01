# =============================================================================
# Council Web — Multi-stage Dockerfile (Next.js 14)
# Target: NVIDIA Ubuntu Linux, rootless Docker / user namespaces
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: deps — install node_modules
# -----------------------------------------------------------------------------
FROM node:22-alpine AS deps

RUN apk add --no-cache libc6-compat

WORKDIR /app

COPY package.json package-lock.json* ./
# Use ci for reproducible installs; falls back gracefully if no lockfile exists
RUN npm ci --prefer-offline || npm install


# -----------------------------------------------------------------------------
# Stage 2: builder — compile Next.js with standalone output
# -----------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app

# Bring in installed modules from deps stage
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# next.config.js must have `output: 'standalone'` for the production stage to
# work.  We patch it here at build time so the source file doesn't need to be
# changed manually.
RUN node -e "
const fs = require('fs');
const path = './next.config.js';
let src = fs.readFileSync(path, 'utf8');
if (!src.includes('standalone')) {
  src = src.replace(
    'const nextConfig = {',
    \"const nextConfig = {\\n  output: 'standalone',\"
  );
  fs.writeFileSync(path, src);
  console.log('Patched next.config.js: added output: standalone');
} else {
  console.log('next.config.js already has standalone output');
}
"

ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

RUN npm run build


# -----------------------------------------------------------------------------
# Stage 3: production — minimal runtime, standalone server
# -----------------------------------------------------------------------------
FROM node:22-alpine AS production

LABEL org.opencontainers.image.title="Council Web"
LABEL org.opencontainers.image.description="Next.js UI for the Council platform"

RUN apk add --no-cache curl

# Non-root user (uid 1001 to avoid collision with host uid 1000 used by API)
RUN addgroup --gid 1001 --system nodejs \
    && adduser --system --uid 1001 --ingroup nodejs nextjs

WORKDIR /app

# Copy only the standalone server output and required public/static assets
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_PUBLIC_API_URL=http://localhost:8000

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:3000/ || exit 1

CMD ["node", "server.js"]
