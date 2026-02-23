"""LLM confidence evaluation — determines if a Mode C response is sufficient."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are evaluating whether a response adequately answers a question. "
    "Respond with only YES or NO."
)


async def evaluate_confidence(
    provider: Any,
    question: str,
    response: str,
    timeout: float = 10.0,
) -> bool:
    """Evaluate whether a response adequately answers a question.

    Uses an LLM provider to make a YES/NO classification.

    Args:
        provider: An Amplifier provider with a complete() method
        question: The original question text
        response: The Mode C response text
        timeout: Maximum time to wait for the evaluation (seconds)

    Returns:
        True if the response is sufficient, False if it should escalate to Mode A.
        Defaults to True on timeout or error (don't escalate on evaluation failure).
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Question: {question}\n\nResponse: {response}"},
    ]

    try:
        result = await asyncio.wait_for(
            provider.complete(messages),
            timeout=timeout,
        )
        # Parse the first word of the response
        answer = ""
        if isinstance(result, str):
            answer = result.strip()
        elif isinstance(result, dict):
            # Handle different provider response formats
            answer = result.get("content", result.get("text", "")).strip()
        elif hasattr(result, "content"):
            answer = str(result.content).strip()
        else:
            answer = str(result).strip()

        first_word = answer.split()[0].upper() if answer else ""
        is_sufficient = first_word.startswith("YES")

        logger.info(
            "Confidence evaluation: %s (raw: %s)",
            "sufficient" if is_sufficient else "insufficient",
            answer[:50],
        )
        return is_sufficient

    except asyncio.TimeoutError:
        logger.warning(
            "Confidence evaluation timed out after %ss — defaulting to sufficient",
            timeout,
        )
        return True
    except Exception as e:
        logger.warning("Confidence evaluation failed: %s — defaulting to sufficient", e)
        return True
