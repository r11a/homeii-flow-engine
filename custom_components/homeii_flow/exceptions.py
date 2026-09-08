"""Exceptions for HOMEii Flow Engine."""

from __future__ import annotations


class HomeiiFlowEngineError(Exception):
    """Base HOMEii Flow Engine error."""


class HomeiiFlowServiceUnavailable(HomeiiFlowEngineError):
    """Raised when a requested Home Assistant service is unavailable."""

