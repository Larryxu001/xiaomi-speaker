# KuGouMusicApi (https://github.com/MakcRe/KuGouMusicApi, MIT) has no published
# image. Built on the server from its source with mainland mirrors:
#   apps/kugou-api/build/build.sh <source.tar.gz> <commit>
FROM docker.m.daocloud.io/library/node:20-alpine
RUN apk add --no-cache tini
ENV NODE_ENV=production COREPACK_NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /app
COPY src/package.json src/pnpm-lock.yaml src/pnpm-workspace.yaml ./
RUN corepack enable && pnpm config set registry https://registry.npmmirror.com \
 && pnpm install --prod --frozen-lockfile
COPY src/ ./
USER node
EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "app.js"]
