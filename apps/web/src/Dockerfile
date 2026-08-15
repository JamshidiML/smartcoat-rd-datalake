FROM python:3.12.11-alpine
WORKDIR /web
COPY . .
RUN adduser -D -u 10003 web && chown -R web:web /web
USER web
EXPOSE 8080
CMD ["python", "server.py"]
