FROM nginx:alpine

COPY nginx.conf.template /nginx.conf.template
COPY index.html /usr/share/nginx/html/index.html

EXPOSE 80

CMD ["/bin/sh", "-c", "envsubst '${API_SECRET}' < /nginx.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
