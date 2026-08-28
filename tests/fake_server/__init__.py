"""A purpose-built downstream MCP server for Sluice's tests.

Every tool here exists to pin a specific behavior or defect. It is deliberately
not a realistic server: it is a fixture whose payload shapes are the interesting
cases from the spec, generated deterministically so tests can assert exact
aggregates against them.
"""

from tests.fake_server.server import PAGE_TWO_TOOL, build_fake_server, rows_payload

__all__ = ["PAGE_TWO_TOOL", "build_fake_server", "rows_payload"]
