FROM node:24-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY src ./src

ENV FAX_API_URL=http://api:8080

EXPOSE 3001

# Streamable HTTP. Each MCP client sends its own Faxbot API key; API_KEY is not used.
CMD ["node", "src/servers/http.js"]
