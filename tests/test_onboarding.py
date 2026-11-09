import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pimcamp.onboarding import SetupValidationError, validate_imap_setup


class SetupValidationTests(unittest.TestCase):
    def settings(self):
        return {"email": "alex@example.test", "account_name": "alex.work@example.test",
                "incoming": {"host": "imap.example.test", "password": " fixture secret "},
                "outgoing": {"host": "smtp.example.test"}}

    def test_arbitrary_account_and_hosts(self):
        result = validate_imap_setup(self.settings())
        self.assertEqual(result.account_name, "alex.work@example.test")
        self.assertEqual(result.incoming.host, "imap.example.test")
        self.assertEqual(result.outgoing.username, "alex@example.test")
        self.assertEqual(result.outgoing.password, " fixture secret ")
        self.assertNotIn("fixture secret", repr(result))
        self.assertNotIn("password", str(result.review()))

    def test_separate_outgoing_login(self):
        value = self.settings()
        value["same_login"] = False
        value["outgoing"].update(username="submission-user", password="other secret",
                                 security="starttls", port="587")
        result = validate_imap_setup(value)
        self.assertEqual(result.outgoing.username, "submission-user")
        self.assertEqual(result.outgoing.port, 587)
        self.assertEqual(result.outgoing.password, "other secret")

    def test_invalid_values_have_field_errors_without_secrets(self):
        value = self.settings()
        value["incoming"].update(host="https://example.test:993", security="none", port=True)
        with self.assertRaises(SetupValidationError) as caught:
            validate_imap_setup(value)
        self.assertEqual(set(caught.exception.fields),
                         {"incoming.host", "incoming.security", "incoming.port"})
        self.assertNotIn("fixture secret", str(caught.exception.fields))

    def test_malformed_types_do_not_escape_as_internal_errors(self):
        for value in (None, [], {"incoming": []}, {"same_login": "false"}):
            with self.subTest(value=value), self.assertRaises(SetupValidationError):
                validate_imap_setup(value)

    def test_no_personal_defaults(self):
        with self.assertRaises(SetupValidationError) as caught:
            validate_imap_setup({})
        self.assertIn("email", caught.exception.fields)
        self.assertIn("incoming.host", caught.exception.fields)
        self.assertIn("outgoing.host", caught.exception.fields)

    def test_human_account_label_and_starttls_defaults(self):
        value = self.settings()
        value["account_name"] = "Work mailbox · alex@example.test"
        value["incoming"]["security"] = "starttls"
        value["outgoing"]["security"] = "starttls"
        result = validate_imap_setup(value)
        self.assertEqual(result.account_name, value["account_name"])
        self.assertEqual(result.incoming.port, 143)
        self.assertEqual(result.outgoing.port, 587)

    def test_account_label_cannot_contain_control_characters(self):
        value = self.settings()
        value["account_name"] = "bad\x00name"
        with self.assertRaises(SetupValidationError) as caught:
            validate_imap_setup(value)
        self.assertIn("account_name", caught.exception.fields)


if __name__ == "__main__":
    unittest.main()
