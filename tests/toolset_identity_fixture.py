"""A request-local identity experiment; this is not used by the bridge."""

from dataclasses import dataclass
import re


VALID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
FAMILIES = frozenset(("browser", "computer"))
PREFIX = "cc_toolset__"


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    family: str | None
    name: str


def alias(identity):
    if not isinstance(identity, Identity) or not isinstance(identity.name, str) or not VALID.fullmatch(identity.name):
        raise IdentityError("Invalid tool identity")
    if identity.family is None:
        return identity.name
    if not isinstance(identity.family, str) or identity.family not in FAMILIES:
        raise IdentityError("Unsupported toolset family")
    encoded = f"{PREFIX}{identity.family}__{identity.name}"
    if not VALID.fullmatch(encoded):
        raise IdentityError("Toolset identity exceeds the fixture name limit")
    return encoded


class RequestAliases:
    """Bind active and replay-only identities for exactly one request.

    Current declarations may not repeat. Historical identities can repeat as
    calls, but an alias can never be rebound to a different identity. Returned
    calls are accepted only for tools offered in this request.
    """

    def __init__(self, current, history=()):
        current = tuple(current)
        history = tuple(history)
        if len(current) > 1000 or len(history) > 10000:
            raise IdentityError("Too many fixture identities")
        aliases = {}
        for identity in current + history:
            name = alias(identity)
            previous = aliases.get(name)
            if previous is not None and previous != identity:
                raise IdentityError("Ambiguous tool identity")
            aliases[name] = identity
        if len(set(current)) != len(current):
            raise IdentityError("Duplicate active tool identity")
        self._aliases = aliases
        self._current = frozenset(current)

    def replay_name(self, identity):
        name = alias(identity)
        if self._aliases.get(name) != identity:
            raise IdentityError("Unknown replay identity")
        return name

    def returned_identity(self, name):
        if not isinstance(name, str):
            raise IdentityError("Invalid returned tool name")
        identity = self._aliases.get(name)
        if identity not in self._current:
            raise IdentityError("Unadvertised or ambiguous returned tool")
        return identity
