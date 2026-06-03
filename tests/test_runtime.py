"""
Unit tests for mdt_rescue.runtime.resolve_ffmpeg_binary (P.1).

Covers the development branch (bare tool name), the frozen branch (bundled
absolute path via sys._MEIPASS), and the two error cases (unsupported tool,
frozen without _MEIPASS).
"""

import os
import sys

import pytest

from mdt_rescue.runtime import resolve_ffmpeg_binary


def test_dev_branch_returns_bare_ffmpeg(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert resolve_ffmpeg_binary("ffmpeg") == "ffmpeg"


def test_dev_branch_returns_bare_ffprobe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert resolve_ffmpeg_binary("ffprobe") == "ffprobe"


def test_frozen_branch_returns_bundled_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/bundle", raising=False)
    assert resolve_ffmpeg_binary("ffmpeg") == os.path.join("/fake/bundle", "ffmpeg")
    assert resolve_ffmpeg_binary("ffprobe") == os.path.join("/fake/bundle", "ffprobe")


def test_invalid_tool_raises_value_error(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    with pytest.raises(ValueError):
        resolve_ffmpeg_binary("python3")


def test_frozen_without_meipass_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    with pytest.raises(RuntimeError):
        resolve_ffmpeg_binary("ffmpeg")
