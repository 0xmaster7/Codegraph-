from __future__ import annotations

try:
    from external_dependency import UnknownExternalBase
except ImportError:
    UnknownExternalBase = object

from .auth import verify_token
from .auth import verify_token as check
from .common import normalize


def callback(value: str) -> bool:
    return bool(value)


def nested_factory(prefix: str):
    def inner(value: str) -> str:
        return prefix + value

    return inner


def _is_nonempty(value: str) -> bool:
    return bool(value)


predicate = lambda value: bool(value)  # noqa: E731 - fixture deliberately includes lambda


def verify_and_normalize(value: str) -> str:
    """Unicode text precedes the call span: 🚀 café λ."""
    if verify_token(value) and check(value):
        return normalize(value)
    return ""


class Base:
    def validate(self, value: str) -> bool:
        return bool(value)


class StrictCheckout(Base):
    @property
    def label(self) -> str:
        return "🚀 café"

    @staticmethod
    def is_supported(value: str) -> bool:
        return bool(value)

    @classmethod
    def from_value(cls, value: str) -> StrictCheckout:
        return cls()

    def validate(self, value: str) -> bool:
        label = "🚀 café λ"
        return check(value) and super().validate(label)


class FastCheckout(Base):
    def validate(self, value: str) -> bool:
        return bool(value)


class UnresolvedCheckout(UnknownExternalBase):  # type: ignore[name-defined]
    pass


class Registry:
    default = StrictCheckout.from_value("class-body call")


MODULE_CHECKOUT = StrictCheckout.from_value("module-level call")


def make_checkout(value: str) -> StrictCheckout:
    return StrictCheckout().validate(value)


def pass_callback(fn=callback):
    return fn
