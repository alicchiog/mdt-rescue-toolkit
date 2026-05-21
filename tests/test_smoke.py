"""
Smoke tests for the mdt_rescue package skeleton.

These tests verify the most basic invariants of the package at
v0.2.0.dev0: that it can be imported, that its declared metadata is
correct, and that the planned subpackage structure is in place.

They do NOT exercise any recovery logic — that is the subject of
the Phase B tests, which will use synthetic .MDT fixtures.
"""

import mdt_rescue


def test_package_imports():
    """The package must be importable without side effects."""
    assert mdt_rescue is not None


def test_version_is_dev_release():
    """At Phase A the version must be the pre-release 0.2.0.dev0."""
    assert mdt_rescue.__version__ == "0.2.0.dev0"


def test_author_metadata():
    """Author metadata must be populated for distribution."""
    assert mdt_rescue.__author__ == "Gianpiero Alicchio"


def test_license_is_mit():
    """The package must declare its MIT license at runtime."""
    assert mdt_rescue.__license__ == "MIT"


def test_all_contains_only_metadata():
    """
    At v0.2.0.dev0 the public API surface intentionally exposes only the
    metadata attributes. No engine/orchestrator/gui re-exports yet.

    Adding new public names here without updating __all__ would be a
    silent expansion of the API surface, so we lock it down.
    """
    expected = {"__version__", "__author__", "__license__"}
    actual = set(mdt_rescue.__all__)
    assert actual == expected, (
        f"__all__ drift detected. Expected {expected}, got {actual}."
    )


def test_placeholder_subpackages_import():
    """Engine and GUI placeholder packages must be importable."""
    from mdt_rescue import engine, gui

    assert engine is not None
    assert gui is not None
