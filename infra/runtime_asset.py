"""Lambda zip contents for handlers that import the ``oerf`` package.

The asset root stays the repository root so ``import oerf`` does not shadow the
stdlib ``platform`` module. ``IgnoreMode.GIT`` makes the leading ``*`` an
allow-list: only ``oerf``, ``domains``, and ``sim`` are copied. ``sim`` stays
because ``FileConfigRepository`` reads that package at runtime through
``importlib.resources``. Every other root is dropped, including ``docs``,
``tests``, ``web``, ``scripts``, ``infra``, ``task_exports``, ``.github``,
``.cursor``, ``.venv``, ``cdk.out``, and ``node_modules``. Inside the three
packages, a basename that starts with ``.`` is dropped, as are ``*.env``,
``.demo-numbers*``, ``__pycache__``, and ``*.pyc``.
"""

from __future__ import annotations

from aws_cdk import IgnoreMode

_PACKAGES = ("oerf", "domains", "sim")

RUNTIME_ASSET_EXCLUDE = [
    "*",
    *[pattern for name in _PACKAGES for pattern in (f"!{name}", f"!{name}/**")],
    "**/.*",
    "**/.env",
    "**/*.env",
    "**/.env.*",
    "**/.demo-numbers*",
    "**/__pycache__",
    "**/*.pyc",
    "**/*.pyo",
]


def runtime_code_args() -> dict[str, object]:
    """Keyword arguments for ``Code.from_asset`` on the shared runtime zip."""

    return {"exclude": RUNTIME_ASSET_EXCLUDE, "ignore_mode": IgnoreMode.GIT}
