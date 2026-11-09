"""Owner-local Google app registration; imported secrets never enter config files."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import uuid

from .onboarding_credentials import CredentialRef
from .onboarding_oauth import GoogleApplication, OAuthSetupError


class GoogleApplicationStore:
    def __init__(self, account_store, credentials, ortie):
        self.store = account_store
        self.credentials = credentials
        self.ortie = ortie
        self.path = account_store.root / 'google-application.json'

    def helper_available(self):
        return bool(self.ortie and Path(self.ortie).is_absolute()
                    and Path(self.ortie).is_file() and os.access(self.ortie, os.X_OK))

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OAuthSetupError('Google app configuration must be an owner-only regular file.')
        try:
            record = json.loads(self.path.read_text())
            ref = CredentialRef(**record['secret_reference'])
            expected_backend = 'secret-service' if self.credentials.lifetime == 'persistent' else 'linux-keyring'
            if ref.backend != expected_backend:
                raise ValueError()
            if not self.helper_available():
                return None
            return GoogleApplication(record['client_id'], self.ortie,
                                     tuple(self.credentials.command(ref)))
        except (KeyError, TypeError, ValueError):
            raise OAuthSetupError('Saved Google app configuration could not be read. Ask the installation owner to check it.') from None

    def status(self):
        application = self.load()
        result = {'configured': application is not None, 'helperAvailable': self.helper_available()}
        if application:
            result['clientId'] = application.client_id
        return result

    def configure(self, raw):
        if not self.helper_available():
            raise OAuthSetupError('The Google sign-in helper is missing. Install Ortie 2.2.0 and reopen setup, then import this file again.')
        if getattr(self.credentials, 'lifetime', None) != 'persistent':
            raise OAuthSetupError('Google app setup needs persistent encrypted password storage. Configure it before importing this file.')
        try:
            if not isinstance(raw, str) or len(raw.encode()) > 32768:
                raise ValueError()
            document = json.loads(raw)
            if not isinstance(document, dict) or 'web' in document:
                raise ValueError()
            client = document['installed']
            client_id, secret = client['client_id'], client['client_secret']
            if not isinstance(secret, str) or not secret or len(secret) > 4096 or '\x00' in secret:
                raise ValueError()
            if not isinstance(client_id, str):
                raise ValueError()
            GoogleApplication(client_id, self.ortie)
        except (ValueError, TypeError, KeyError, OAuthSetupError):
            raise OAuthSetupError('Choose the JSON file downloaded for a Google Desktop app client, including its client ID and client secret. Web and service-account files are not supported.') from None
        with self.store._lock():
            existing = self.load()
            if existing:
                if existing.client_id == client_id:
                    return existing  # Lost-response retry; never create another secret.
                raise OAuthSetupError('A different Google app is already configured. Existing accounts were not changed.')
            reference = self.credentials.create(str(uuid.uuid4()), 'oauth', secret)
            temporary = self.path.with_name('.google-application-' + uuid.uuid4().hex)
            published = False
            try:
                record = {'client_id': client_id, 'secret_reference': asdict(reference)}
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    json.dump(record, output)
                    output.flush()
                    os.fsync(output.fileno())
                # Never overwrite an existing installation's application.
                os.link(temporary, self.path)
                published = True
                directory = os.open(self.store.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
                if not published and not self.credentials.remove(reference):
                    raise OAuthSetupError('App setup failed and its temporary credential could not be removed. Ask the installation owner to check storage.')
            return GoogleApplication(client_id, self.ortie, tuple(self.credentials.command(reference)))
