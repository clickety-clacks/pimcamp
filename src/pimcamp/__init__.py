"""Pimcamp strict MVP implementation."""

CONTRACT_VERSION = "pimcamp.v1"
CAPABILITIES = frozenset(
    {"list", "read", "compose", "reply", "send", "junk", "subscribe_new_mail"}
)
