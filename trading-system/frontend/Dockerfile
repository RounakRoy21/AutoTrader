# ── Stage 1: Build Angular ────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npx ng build --configuration production

# ── Stage 2: Development (ng serve with HMR + proxy) ─────────────────────────
FROM node:20-alpine AS dev
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
EXPOSE 4200
CMD ["npx", "ng", "serve", "--host", "0.0.0.0", "--port", "4200", "--poll", "2000"]

# ── Stage 3: Production (nginx serving the compiled app) ─────────────────────
FROM nginx:stable-alpine AS production
# Angular 17+ builder emits to dist/<project>/browser/
COPY --from=builder /app/dist/autotrader-dashboard/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
