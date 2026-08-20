# Sentinel web — Next.js on Node 22
FROM node:22-bookworm-slim

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app/apps/web

COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci || npm install

COPY apps/web ./

RUN chown -R node:node /app
USER node

EXPOSE 3000
CMD ["npm", "run", "dev"]
