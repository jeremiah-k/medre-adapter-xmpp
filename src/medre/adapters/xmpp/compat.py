"""Probe SDK installation only; fake and configuration paths avoid importing it."""

from importlib.util import find_spec

HAS_SLIXMPP = find_spec("slixmpp") is not None
