# llm.py - Observability: one choke-point for every Gemini call.
#
# Nothing in the app calls client.models.generate_content() directly — it
# calls tracked_generate(), which records prompt, response, token counts →
# cost, latency, and any error for every call, as one JSON line appended to
# llm_calls.jsonl and as a Langfuse generation.

import datetime
import json
import os
import time

from google import genai
from dotenv import load_dotenv
from langfuse import get_client

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Langfuse batches events in an in-memory queue; unsent events die with
# the process (hence langfuse.shutdown() on app shutdown in main.py).
langfuse = get_client()  # reads LANGFUSE_* from .env

TRACE_FILE = os.getenv("LLM_TRACE_FILE", "llm_calls.jsonl")

# Prices change, so they live in .env as USD per 1M tokens.
PRICE_IN_PER_1M = float(os.getenv("PRICE_IN_PER_1M", "0"))
PRICE_OUT_PER_1M = float(os.getenv("PRICE_OUT_PER_1M", "0"))


def tracked_generate(model: str, prompt: str, purpose: str, **meta) -> str:
    """Call Gemini and leave a trace record no matter what happens.

    purpose  — which part of the app is calling ("classify", "write_report").
               This is what lets you later ask "where does my money go?"
    **meta   — anything else worth attaching (log_id, attempt number...).
    """
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = time.perf_counter()

    # Attaches to whatever span is "current" — the @observe trace opened
    # by the caller, or an auto-created flat trace if none.
    gen = langfuse.start_observation(
        name=purpose, as_type="generation", input=prompt, metadata=meta,
    )

    text, usage, error = None, {}, None
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        text = response.text
        u = response.usage_metadata
        usage = {
            "prompt_tokens": u.prompt_token_count or 0,
            "output_tokens": u.candidates_token_count or 0,
            # Thinking models bill reasoning tokens at the OUTPUT rate, but
            # they're NOT inside candidates_token_count — miss this field and
            # every cost figure quietly undercounts.
            "thinking_tokens": getattr(u, "thoughts_token_count", 0) or 0,
            "total_tokens": u.total_token_count or 0,
        }
        return text
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        gen.update(level="ERROR", status_message=error)
        raise  # tracing must OBSERVE failures, not swallow them
    finally:
        # `finally` guarantees a trace line even when the call blows up —
        # failed calls are the ones you most need visibility into.
        latency_ms = round((time.perf_counter() - t0) * 1000)
        billed_out = usage.get("output_tokens", 0) + usage.get("thinking_tokens", 0)
        cost_usd = round(
            usage.get("prompt_tokens", 0) / 1e6 * PRICE_IN_PER_1M
            + billed_out / 1e6 * PRICE_OUT_PER_1M,
            6,
        )
        record = {
            "ts": started,
            "purpose": purpose,
            "model": model,
            "latency_ms": latency_ms,
            **usage,
            "cost_usd": cost_usd,
            "error": error,
            "prompt": prompt,
            "response": text,
            **meta,
        }
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        # cost_details keeps OUR .env prices authoritative; later this can
        # move to Langfuse's model price table.
        gen.update(
            model=model,
            output=text,
            usage_details={
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "reasoning": usage.get("thinking_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
            cost_details={"total": cost_usd},
        )
        gen.end()
        print(f"   📊 [{purpose}] {usage.get('total_tokens', '?')} tok, "
              f"{latency_ms}ms, ${cost_usd}" + (f"  ⚠ {error}" if error else ""))
