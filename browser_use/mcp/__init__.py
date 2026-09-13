"""MCP (Model Context Protocol) support for browser-use.

This module provides integration with MCP servers and clients for browser automation.
``server.py`` is a custom MCP server (``BuMcpServer``) replacing the original one --
see its module docstring for why.
"""

from browser_use.mcp.client import MCPClient
from browser_use.mcp.controller import MCPToolWrapper

__all__ = ['MCPClient', 'MCPToolWrapper', 'BuMcpServer']  # type: ignore


def __getattr__(name):
	"""Lazy import to avoid importing server module when only client is needed."""
	if name == 'BuMcpServer':
		from browser_use.mcp.server import BuMcpServer

		return BuMcpServer
	raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
