"""Shared helpers; normalize intentionally collides with another module."""


def normalize(value: str) -> str:
    return value.strip().lower()
