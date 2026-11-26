ARG CELLD_VERSION=v0.6.3
FROM ghcr.io/denoland/celld:${CELLD_VERSION} AS celld-binary

FROM node:24-bookworm-slim AS app

RUN apt-get update \
	&& apt-get install -y --no-install-recommends ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

COPY --from=celld-binary /usr/local/bin/celld /usr/local/bin/celld

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

ENV PATH="/app/node_modules/.bin:${PATH}"

EXPOSE 8080

ENTRYPOINT ["celld"]
CMD ["dev", "wrangler.jsonc", "--host", "0.0.0.0", "--port", "8080", "--no-watch"]
