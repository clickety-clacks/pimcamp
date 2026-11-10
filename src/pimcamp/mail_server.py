"""Single-process loopback hosting for the private mail WSGI application."""

from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
import argparse
from pathlib import Path
import signal

from .config import Config, load
from .errors import PimcampError
from .mail_http import MailHTTPApplication


class ServeIdentityVerifier:
    """Consume Serve's replaced assertion only at the trusted loopback ingress.

    This is not a general-purpose header authentication scheme. The deployment
    must expose this listener only through its existing private Serve proxy.
    """

    def authenticate(self, headers: dict[str, str]) -> str | None:
        return headers.get("tailscale-user-login") or None


def create_application(config: Config) -> MailHTTPApplication:
    if config.mail_origin is None:
        raise ValueError("The private HTTPS mail origin must be configured.")
    # Startup snapshot: changes require restarting the runner. There are no
    # grants derived from CLI credentials, saved accounts or network membership.
    grants = dict(config.mail_principals)
    return MailHTTPApplication(
        config, ServeIdentityVerifier(), lambda principal: grants.get(principal, ()),
    )


class _PrivateRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # The default access log includes the requested message locator. Do not
        # persist private paths, query strings or request headers in that log.
        pass


def create_server(application: MailHTTPApplication, port: int) -> WSGIServer:
    """Mount the real routes on IPv4 loopback; authentication stays in the app.

    This deliberately has no public bind option and no threaded/multiprocess
    mode: mail sessions belong to this process. Port zero is useful for isolated
    fixtures; a production launcher must supply its explicitly chosen port.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("The mail listener port is invalid.")
    return make_server(
        "127.0.0.1", port, application, handler_class=_PrivateRequestHandler
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve private Pimcamp mail on loopback behind Serve.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Choose an explicit port from 1 to 65535.")
    try:
        application = create_application(load(args.config))
        server = create_server(application, args.port)
    except (PimcampError, ValueError, OSError):
        parser.error("The private mail configuration or loopback listener is unavailable.")

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
