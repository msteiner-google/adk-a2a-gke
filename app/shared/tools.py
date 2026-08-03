"""Reusable tools shared across agent variants.

Mocked example logic — put tools here that every variant should expose. Anything
variant-specific stays in that variant's own `app/tools.py`.
"""


def echo(text: str) -> str:
    """Echo the provided text back, tagged as coming from the shared library.

    Placeholder shared tool — replace with real reusable tools (e.g. a company
    knowledge-base lookup, an internal API client, etc.).

    Args:
        text: The text to echo back.

    Returns:
        The input text, prefixed to make it obvious it came from the shared lib.
    """
    return f"[shared] {text}"
