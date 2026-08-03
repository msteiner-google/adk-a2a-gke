"""Tools specific to the research agent."""

from __future__ import annotations


def web_search(query: str) -> dict[str, str]:
    """Look up information for a query (deterministic placeholder).

    This is a stub so the research agent works out of the box and in hermetic
    tests. Replace it with a real search/RAG integration for your use case.

    Args:
        query: The search query.

    Returns:
        A mapping echoing the query with a placeholder result.
    """
    return {
        "status": "ok",
        "query": query,
        "result": f"No live search configured; replace web_search to answer: {query}",
    }
