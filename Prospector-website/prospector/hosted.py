"""Single-owner deployment behind an HTTPS reverse proxy."""

import base64
import hmac
import os
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from .web import Handler


class HostedHandler(Handler):
    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "").encode()
        return hmac.compare_digest(authorization, self.server.authorization)

    def _same_origin(self) -> bool:
        return self.headers.get("Origin") == self.server.origin

    def _send(self, code, body, content_type, extra=None):
        headers = {"Cache-Control": "no-store", **(extra or {})}
        headers.pop("Set-Cookie", None)
        if code == 401:
            headers["WWW-Authenticate"] = 'Basic realm="Prospector", charset="UTF-8"'
            body = b"Sign in as prospector."
            content_type = "text/plain; charset=utf-8"
        super()._send(code, body, content_type, headers)


def main() -> None:
    origin = os.environ["PROSPECTOR_PUBLIC_ORIGIN"]
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or origin != f"https://{parsed.netloc}" or parsed.username:
        raise SystemExit("PROSPECTOR_PUBLIC_ORIGIN must be the exact HTTPS origin.")
    password = os.environ["PROSPECTOR_WEB_PASSWORD"]
    if len(password) < 20:
        raise SystemExit("Use a random PROSPECTOR_WEB_PASSWORD of at least 20 characters.")
    with ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HostedHandler) as server:
        server.origin = origin
        server.authorization = b"Basic " + base64.b64encode(f"prospector:{password}".encode())
        server.serve_forever()


if __name__ == "__main__":
    main()
