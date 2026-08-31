"""
Backend-for-frontend for the goodreads-stats web chat.

A separate top-level package, not part of `goodreads_mcp`, for three reasons:

  * The MCP server must stay IAM-private; this service must be reachable by a
    browser. One Cloud Run service has one IAM policy, so they cannot be the
    same service.
  * `goodreads_mcp` bans every write to stdout, enforced by an AST test that
    walks `goodreads_mcp.__path__`. A uvicorn app belongs outside that walk
    rather than inside it with an exemption.
  * Privilege separation: this package's service account holds no BigQuery
    role at all. It can reach BigQuery only as a consequence of an MCP tool
    call executed under the MCP service's own identity.

It imports exactly two things from the server package, both pure and neither
touching credentials: the caveat registry (`caveats`) and the query guard
(`bq.guard`). Nothing here re-states a fact the server already owns.
"""

__all__ = ["config"]
