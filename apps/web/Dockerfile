# Local container for the SPA (AWS serves the built assets from S3 + CloudFront instead).
FROM node:22.20.0-alpine AS build
WORKDIR /web
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29.1-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
