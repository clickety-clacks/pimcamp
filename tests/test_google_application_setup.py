from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pimcamp.onboarding_credentials import CredentialRef
from pimcamp.onboarding_google_application import GoogleApplicationStore
from pimcamp.onboarding_oauth import OAuthSetupError
from pimcamp.onboarding_store import AccountStore
from pimcamp.onboarding_service import SetupService


class ProtectedFixture:
    lifetime = 'persistent'

    def __init__(self):
        self.values = {}

    def create(self, revision, purpose, secret):
        reference = CredentialRef(revision, None, 'secret-service')
        self.values[revision] = secret
        return reference

    def command(self, reference):
        return ['/fixture/credential-reader', reference.entry]

    def remove(self, reference):
        self.values.pop(reference.entry, None)
        return True


class GoogleApplicationSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = AccountStore(self.root / 'accounts')
        self.credentials = ProtectedFixture()
        self.registry = GoogleApplicationStore(self.store, self.credentials, '/bin/true')
        self.client_id = '123-fixture.apps.googleusercontent.com'
        self.raw = json.dumps({'installed': {'client_id': self.client_id,
                                           'client_secret': 'fixture-app-secret'}})

    def test_persists_reference_only_and_loads_after_restart(self):
        application = self.registry.configure(self.raw)
        self.assertEqual(application.client_id, self.client_id)
        self.assertEqual(len(self.credentials.values), 1)
        self.assertNotIn('fixture-app-secret', self.registry.path.read_text())
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o600)
        restarted = GoogleApplicationStore(self.store, self.credentials, '/bin/true')
        self.assertEqual(restarted.load(), application)
        self.assertEqual(restarted.status(), {'configured': True, 'helperAvailable': True,
                                              'clientId': self.client_id})
        self.assertEqual(self.store.list_accounts(), [])

    def test_retry_is_idempotent_and_different_app_is_not_overwritten(self):
        first = self.registry.configure(self.raw)
        self.assertEqual(self.registry.configure(self.raw), first)
        self.assertEqual(len(self.credentials.values), 1)
        with self.assertRaises(OAuthSetupError):
            self.registry.configure(self.raw.replace('123-fixture', '456-other'))
        self.assertEqual(self.registry.load(), first)

    def test_invalid_imports_never_create_credentials(self):
        for raw in ('broken', '{}', '[]', self.raw.replace('installed', 'web'),
                    self.raw.replace(self.client_id, 'not-an-id'), 'a' * 33000):
            with self.subTest(raw=raw[:20]), self.assertRaises(OAuthSetupError):
                self.registry.configure(raw)
        self.assertEqual(self.credentials.values, {})

    def test_missing_helper_and_volatile_storage_are_actionable(self):
        self.registry.ortie = None
        self.assertFalse(self.registry.status()['helperAvailable'])
        with self.assertRaisesRegex(OAuthSetupError, 'helper is missing'):
            self.registry.configure(self.raw)
        self.registry.ortie = '/bin/true'
        self.credentials.lifetime = 'session'
        with self.assertRaisesRegex(OAuthSetupError, 'persistent encrypted'):
            self.registry.configure(self.raw)

    def test_failed_publication_removes_only_owned_secret(self):
        self.credentials.values['existing'] = 'leave-alone'
        with patch('pimcamp.onboarding_google_application.os.link', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.registry.configure(self.raw)
        self.assertEqual(self.credentials.values, {'existing': 'leave-alone'})
        self.assertFalse(self.registry.path.exists())
        self.assertEqual(list(self.store.root.glob('.google-application-*')), [])

    def test_symlink_config_is_rejected(self):
        self.registry.path.symlink_to(self.root / 'missing')
        with self.assertRaises(OAuthSetupError):
            self.registry.load()

    def test_service_enables_google_without_connecting_or_losing_attempt(self):
        service = SetupService(self.store, self.credentials, self.root / 'state',
                               '/fixture/himalaya', '/fixture/carillon',
                               google_application_store=self.registry)
        attempt = service.begin()['setupId']
        self.assertFalse(service.google_application_status()['configured'])
        self.assertTrue(service.configure_google_application(self.raw)['configured'])
        self.assertIn(attempt, service.attempts)
        self.assertTrue(service.begin()['installation']['oauthClientConfigured'])
        self.assertEqual(service.list_accounts(), [])
