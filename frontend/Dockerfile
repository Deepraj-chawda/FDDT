FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# nginx substitutes ${API_UPSTREAM} and the body limits into the template at
# container start. The body limits must cover the largest per-company upload
# limit (Platform > Companies); raise them at deploy time with -e when a
# company is given more.
ENV UPLOAD_BODY_LIMIT=11m     BULK_UPLOAD_BODY_LIMIT=301m
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
