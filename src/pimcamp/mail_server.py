"""Single-process loopback hosting for the private mail WSGI application."""

from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .mail_http import MailHTTPApplication


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
