from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src http://127.0.0.1:8000; img-src 'self' blob:; frame-src blob:; object-src 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
