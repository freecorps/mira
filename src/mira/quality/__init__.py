"""Review-quality measurement: backtests over history and escaped-bug tracking.

``mira backtest`` replays the review engine over merged pull requests without
posting anything and scores what it finds against what happened afterwards.
Escaped-bug tracking watches for reverts and hotfixes and links each one back
to the pull request Mira reviewed, which yields a real-world recall figure.

Deliberately import-light: ``mira.config`` imports the config models from this
package, so nothing here may import ``mira.config`` at module level.
"""
