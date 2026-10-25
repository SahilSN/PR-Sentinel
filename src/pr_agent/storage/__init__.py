"""Pluggable storage for review state (replaces the base64 block in the PR comment)."""

from .json_store import JsonStateStore

__all__ = ["JsonStateStore"]
