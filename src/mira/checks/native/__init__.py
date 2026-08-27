"""Native, deterministic pre-merge checks.

Each module here answers one question from the diff and the pull request
metadata alone. None of them calls a model, and none of them runs a
subprocess: a native check is the cheapest thing in the framework and is
expected to run on every pull request on a four-core board.
"""
