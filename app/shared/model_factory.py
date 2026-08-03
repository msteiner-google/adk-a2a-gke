"""Factory for configured ADK `Gemini` models.

Centralizes model construction so every variant gets the same retry/backoff
policy and (optionally) the same geofenced genai client.
"""

from google.adk.models import Gemini
from google.genai import Client, types


def build_model(model: str, client: Client | None = None) -> Gemini:
    """Build a Gemini model configured with our standard retry policy.

    Centralizing this here means all variants get the same retry/backoff
    behavior, and changing it once updates every variant. Pass a `client` to
    pin the model to a specific Vertex AI project/location (see `ModelModule`).

    Args:
        model: The Gemini model name to use.
        client: An optional pre-built `google.genai.Client`. When provided it is
            patched onto the model so all calls go through that client (and its
            project/location); when omitted the model uses ADK's default client.

    Returns:
        A configured `Gemini` model instance.
    """
    model_instance = Gemini(
        model=model,
        retry_options=types.HttpRetryOptions(attempts=3),
    )
    if client is not None:
        # Route the model through the geofenced Vertex AI client.
        model_instance.api_client = client
    return model_instance
