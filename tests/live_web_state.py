"""Probe hosted search and native-state replay on the existing subscription.

Consumes subscription quota. No API project key or user content is used. Prints
only counters and pass/fail metadata, never encrypted state or response bodies.
"""

import argparse
import json
from pathlib import Path
import re
import uuid

from live_support import existing_subscription, temporary_translator
from live_compare_translators import stream
import bridge_models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path); parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"])["sol"]
    base = {"model": model, "max_tokens": 500, "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
            "metadata": {"user_id": json.dumps({"session_id": str(uuid.uuid4())})},
            "tools": [{"type": "web_search_20260209", "name": "web_search"}]}
    messages = [{"role": "user", "content": "Search for the official OpenAI Codex pricing documentation. Return one official source URL only."}]
    with temporary_translator(args.binary.resolve(), upstream, model) as (port, key):
        content, first = stream(port, key, {**base, "messages": messages,
                                          "tool_choice": {"type": "tool", "name": "web_search"}})
    calls = [b for b in content if b.get("type") == "server_tool_use"]
    carriers = [b for b in content if b.get("signature", "").startswith("codex_web_search:v1:")]
    sources = [s for b in content if b.get("type") == "web_search_tool_result"
               for s in b.get("content", []) if isinstance(s, dict) and s.get("url")]
    if not calls or not carriers:
        raise RuntimeError("Hosted search did not return replayable native state")
    returned_urls = set(re.findall(r'https://[^\s<>\[\]"()]+', "".join(b.get("text", "") for b in content)))
    returned_urls.update(s["url"] for s in sources)
    print(json.dumps({"stage": "search", "hosted_search_calls": len(calls), "source_count": len(sources),
                      "returned_url_count": len(returned_urls), "native_state_carriers": len(carriers),
                      "metrics": first}), flush=True)
    messages.extend([{"role": "assistant", "content": content}, {"role": "user",
                     "content": "Without another search, repeat one official source URL from the previous result."}])
    with temporary_translator(args.binary.resolve(), upstream, model) as (port, key):
        answer, second = stream(port, key, {**base, "messages": messages, "tool_choice": {"type": "none"}})
    text = "".join(b.get("text", "") for b in answer)
    if not any(url in text for url in returned_urls):
        raise RuntimeError("Follow-up did not retain an exact returned source URL")
    print(json.dumps({"model": model, "hosted_search_calls": len(calls), "source_count": len(sources),
                      "native_state_carriers": len(carriers), "source_recall_after_new_proxy": "pass",
                      "native_source_url_recalled": any(s["url"] in text for s in sources),
                      "search_metrics": first, "recall_metrics": second}))


if __name__ == "__main__":
    main()
