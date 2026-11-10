"""Pimcamp strict MVP implementation."""

CONTRACT_VERSION = "pimcamp.v1"
CAPABILITIES = frozenset(
    {"list", "read", "compose", "reply", "send", "junk", "subscribe_new_mail"}
)
V2_CONTRACT_VERSION = "pimcamp.v2"
V2_CAPABILITIES = frozenset({"accounts", "account_info", "list_account", "issue_link"})
ALL_CAPABILITIES = CAPABILITIES | V2_CAPABILITIES
