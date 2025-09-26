FROM node:18-alpine as build
WORKDIR /ui
COPY package*.json ./
RUN npm ci --prefer-offline --no-audit
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /ui/dist /usr/share/nginx/html


