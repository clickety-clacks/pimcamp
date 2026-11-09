"""Headless-browser integration fixture: never contacts mail or stores real keys."""
from pathlib import Path
import json
import signal
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from test_google_application_setup import ProtectedFixture
from pimcamp.onboarding_google_application import GoogleApplicationStore
from pimcamp.onboarding_http import SetupHTTPServer
from pimcamp.onboarding_service import SetupService
from pimcamp.onboarding_store import AccountStore


def main():
    with tempfile.TemporaryDirectory(prefix="pimcamp-web-fixture-") as temporary:
        root = Path(temporary)
        credentials = ProtectedFixture()
        def checker(_executable, config, _account, backend):
            if not config.is_file():
                raise AssertionError("Missing staged configuration")
            return {"ok": True, "message": f"Fixture {backend} check passed; no mail server contacted."}
        accounts = AccountStore(root / "config")
        registry = GoogleApplicationStore(accounts, credentials, "/usr/bin/true")
        service = SetupService(accounts, credentials, root / "state",
                               "/fixture/himalaya", "/fixture/carillon", checker=checker,
                               google_application_store=registry)
        server = SetupHTTPServer(service, ROOT / "ui/onboarding", root / "launch", port=0)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps({"fixture": True, "launch_file": str(server.launch_file), "origin": server.origin}), flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            records = service.list_accounts()
            print(json.dumps({"fixture_accounts_saved": len(records), "fixture_keys": len(credentials.values)}), flush=True)
            for setup_id in list(service.attempts):
                service.cancel(setup_id)


if __name__ == "__main__":
    main()
