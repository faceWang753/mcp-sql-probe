"""Checks for MCP servers that expose a SQL query tool."""

from .probe import Check, Probe, ProbeError, Report, run_probe

__all__ = ["Check", "Probe", "ProbeError", "Report", "run_probe"]
__version__ = "0.1.0"
