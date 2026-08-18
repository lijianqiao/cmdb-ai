# 前端镜像：Vite 构建产物 + nginx 静态托管与 API 反代
#
# 不把后端地址烤进构建产物：src/lib/api.ts 的 baseURL 默认是相对路径 /api/v1，
# WS 地址由页面 host 推出，所以同源反代之后同一份镜像能部署到任意域名。

FROM node:24-alpine AS builder

WORKDIR /app

# 先只装依赖，package*.json 不变时复用缓存
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# build 脚本是 `tsc -b && vite build`，类型错误会直接让构建失败
RUN npm run build


FROM nginx:alpine AS runtime

# map 只能放在 http 块里，所以单独落一个 conf.d 之外的片段
RUN printf 'map $http_upgrade $connection_upgrade {\n  default upgrade;\n  "" close;\n}\n' \
    > /etc/nginx/conf.d/upgrade-map.conf

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80
