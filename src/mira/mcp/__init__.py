"""Mira's read-only MCP server.

An agent that can already read a repository still cannot see what Mira thinks
about it: which findings were raised and whether they held up, which rules a
human approved, how those rules have performed since, what the index knows
about a file. This package hands that over, and only that.

Three properties hold it together, and each one is a place a read-only surface
usually goes wrong.

**It cannot write.** Not by convention - by inventory. The registry in `tools`
holds seven functions, all of which run a SELECT and build a dictionary. There
is no approve, no dismiss, no re-review, no autofix, no command. A store is
opened only when its file already exists, because connecting would create one.

**It reads the repositories it was granted and no others.** The grant is built
at startup from the configuration the *server* was launched with. A tool call
names a repository and that name is looked up in the grant rather than parsed
into one, so nothing a client sends - a different owner, a path that walks out
of the index directory, a spelling that would open another store - reaches the
data. An enabled server with no repositories configured refuses everything.

**What it returns is data, and says so.** Everything stored came out of a
repository, and the reader on the other end is a model. So every response is
redacted with the same filter autofix uses, then wrapped in one delimited block
that announces itself as content and that the content cannot close.

And every call - answered or refused - is recorded, because a surface that
changes nothing leaves no other trace of having been used.
"""

from mira.mcp.authz import Grant, NotAuthorized, Repository, parse_repository
from mira.mcp.server import MiraMcpServer

__all__ = ["Grant", "MiraMcpServer", "NotAuthorized", "Repository", "parse_repository"]
