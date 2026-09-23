FROM node:22.23.4-alpine AS builder

WORKDIR /app

RUN apk add --no-cache git yarn

COPY package.json yarn.lock* ./
RUN --mount=type=cache,target=/usr/local/share/.cache/yarn \
	yarn install --ignore-scripts --non-interactive

COPY . .
RUN find src scripts -type d \( -name '__tests__' -o -name '__mocks__' \) -prune -exec rm -rf {} + && \
	find src scripts -type f \( -name '*.test.ts' -o -name '*.test.js' \) -delete && \
	yarn build

# Keep only runtime dependencies in the layer copied to the final image.
RUN --mount=type=cache,target=/usr/local/share/.cache/yarn \
	yarn install --production --ignore-scripts --non-interactive --no-lockfile && \
	rm -rf /root/.cache /tmp/*

FROM node:22.23.4-alpine

WORKDIR /app

ENV NODE_ENV=production
ENV PORT=4104

COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/scripts/migrate-deploy.js ./scripts/migrate-deploy.js
COPY --from=builder /app/src/drizzle/migrations ./src/drizzle/migrations

EXPOSE 4104

CMD ["node", "dist/server.js"]
