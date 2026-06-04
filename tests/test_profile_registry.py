"""Tests for the additive profile registry in :mod:`mdt_rescue.profiles`.

P4.1: locks the explicit single-profile registry around the validated
GH5S profile -- default selection, name lookup, and the unknown-profile
error -- without changing any recovery behavior or recovery bytes.

Profile objects are compared by identity (``is``): :class:`Profile` is a
frozen dataclass, so two equal-valued instances could compare ``==``.
These tests guarantee the registry, the default, and ``recover`` all
point at the same canonical object.
"""

from __future__ import annotations

import inspect

import pytest

from mdt_rescue.orchestrator import recover
from mdt_rescue.profiles import (
    DEFAULT_PROFILE,
    GH5S_FHD25_ALLI_200M,
    PROFILES,
    UnknownProfileError,
    get_profile,
)


class TestDefaultProfile:
    """The default profile is the canonical GH5S object."""

    def test_default_profile_is_gh5s_by_identity(self):
        assert DEFAULT_PROFILE is GH5S_FHD25_ALLI_200M


class TestRegistryContents:
    """The registry holds exactly the one validated profile."""

    def test_registry_has_exactly_one_profile(self):
        assert len(PROFILES) == 1

    def test_registry_key_is_canonical_name(self):
        assert list(PROFILES.keys()) == [DEFAULT_PROFILE.name]

    def test_registry_holds_only_the_gh5s_profile(self):
        only_profile = next(iter(PROFILES.values()))
        assert only_profile is GH5S_FHD25_ALLI_200M


class TestGetProfile:
    """Name lookup and the unknown-profile error contract."""

    def test_valid_lookup_returns_canonical_object(self):
        assert get_profile(DEFAULT_PROFILE.name) is GH5S_FHD25_ALLI_200M

    def test_unknown_profile_raises_UnknownProfileError(self):
        with pytest.raises(UnknownProfileError):
            get_profile("definitely-not-a-profile")

    def test_unknown_profile_error_is_a_ValueError(self):
        # Architectural decision: a failed lookup is an API/usage error
        # (ValueError), NOT a RecoveryError absorbed by recover().
        assert issubclass(UnknownProfileError, ValueError)
        with pytest.raises(ValueError):
            get_profile("definitely-not-a-profile")

    def test_unknown_profile_message_includes_requested_name(self):
        with pytest.raises(UnknownProfileError) as excinfo:
            get_profile("definitely-not-a-profile")
        assert "definitely-not-a-profile" in str(excinfo.value)


class TestRecoverDefault:
    """recover()'s default profile is GH5S, checked by identity only."""

    def test_recover_default_profile_is_gh5s_by_identity(self):
        sig = inspect.signature(recover)
        default = sig.parameters["profile"].default
        assert default is GH5S_FHD25_ALLI_200M
