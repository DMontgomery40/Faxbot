FROM node:18-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install --production

COPY src ./src

ENV FAX_API_URL=http://api:8080
ENV API_KEY=

EXPOSE 3001 3002 3004

# Default to HTTP (streamable) transport; override CMD to run SSE instead.
CMD ["node", "src/servers/http.js"]
