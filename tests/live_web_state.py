"""Probe hosted search and native-state replay on the existing subscription.

Consumes subscription quota. No API project key or user content is used. Prints
only counters and pass/fail metadata, never encrypted state or response bodies.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
import uuid

from live_support import existing_subscription, temporary_translator
from live_compare_translators import stream
import bridge_models
import rolling_runtime


def without_tracking(value):
    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.urlencode([(key, item) for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                                   if not key.lower().startswith("utm_")])
    return urllib.parse.urlunsplit(parsed._replace(query=query, fragment=""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?"); parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--managed", action="store_true", help="Use the installed managed TLS endpoint")
    parser.add_argument("--handoff", action="store_true", help="Reload the managed backend between search and recall")
    parser.add_argument("--require-citations", action="store_true", help="Require native URL citation metadata as well as search results")
    args = parser.parse_args()
    if not args.managed and args.binary is None: parser.error("Supply a proxy binary or --managed")
    if args.handoff and not args.managed: parser.error("--handoff requires --managed")
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"])["sol"]
    managed = rolling_runtime.state(args.root) if args.managed else None

    @contextmanager
    def endpoint():
        if args.managed:
            if managed is None: raise RuntimeError("Managed runtime is not activated")
            rolling_runtime.endpoint_status(args.root, managed)
            runtime = args.root / "runtime-managed"
            yield managed["port"], rolling_runtime.private_key(runtime / "front-key"), runtime / "ca.pem"
        else:
            with temporary_translator(args.binary.resolve(), upstream, model) as (port, key):
                yield port, key, None
    base = {"model": model, "max_tokens": 500, "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
            "metadata": {"user_id": json.dumps({"session_id": str(uuid.uuid4())})},
            "tools": [{"type": "web_search_20260209", "name": "web_search"}]}
    messages = [{"role": "user", "content": "Search for the official OpenAI Codex pricing documentation. Return one official source URL only."}]
    if args.require_citations:
        messages[0]["content"] = "Search the official OpenAI Codex pricing documentation. Write one short sentence about included ChatGPT-plan Codex usage, with a native URL citation to an official source."
    with endpoint() as (port, key, ca):
        content, first = stream(port, key, {**base, "messages": messages,
                                          "tool_choice": {"type": "tool", "name": "web_search"}}, ca_file=ca)
    calls = [b for b in content if b.get("type") == "server_tool_use"]
    carriers = [b for b in content if b.get("signature", "").startswith("codex_web_search:v1:")]
    sources = [s for b in content if b.get("type") == "web_search_tool_result"
               for s in b.get("content", []) if isinstance(s, dict) and s.get("url")]
    if not calls or not carriers:
        raise RuntimeError("Hosted search did not return replayable native state")
    citations = [citation for block in content if block.get("type") == "text" for citation in block.get("citations", [])]
    if args.require_citations and not citations:
        raise RuntimeError("No native URL citation was observed; preservation is not established")
    if any(citation.get("cited_text") != "" or not citation.get("encrypted_index", "").startswith("codex_url_citation:v1:") for citation in citations):
        raise RuntimeError("Citation provenance or source-quotation contract failed")
    returned_urls = set(re.findall(r'https://[^\s<>\[\]"()]+', "".join(b.get("text", "") for b in content)))
    returned_urls.update(s["url"] for s in sources)
    returned_urls.update(citation["url"] for citation in citations)
    print(json.dumps({"stage": "search", "hosted_search_calls": len(calls), "source_count": len(sources),
                      "returned_url_count": len(returned_urls), "native_state_carriers": len(carriers),
                      "url_citations": len(citations),
                      "metrics": first}), flush=True)
    messages.extend([{"role": "assistant", "content": content}, {"role": "user",
                     "content": "Without another search, repeat one official source URL from the previous result."}])
    if args.handoff:
        run = subprocess.run([sys.executable, str(args.root / "launcher.py"), "bridge", "reload"], capture_output=True,
                             env={**os.environ, "CLAUDE_CODEX_BRIDGE_HOME": str(args.root)}, timeout=60)
        if run.returncode: raise RuntimeError("Managed search probe reload failed; raw output withheld")
        after = rolling_runtime.state(args.root)
        if (after["pid"], after["port"]) != (managed["pid"], managed["port"]):
            raise RuntimeError("Stable managed endpoint changed during search probe")
    with endpoint() as (port, key, ca):
        answer, second = stream(port, key, {**base, "messages": messages, "tool_choice": {"type": "none"}}, ca_file=ca)
    text = "".join(b.get("text", "") for b in answer)
    if not any(url in text for url in returned_urls):
        raise RuntimeError("Follow-up did not retain an exact returned source URL")
    print(json.dumps({"model": model, "hosted_search_calls": len(calls), "source_count": len(sources),
                      "managed": args.managed, "backend_handoff": args.handoff,
                      "native_state_carriers": len(carriers), "source_recall_after_new_proxy": "pass",
                      "url_citations": len(citations),
                      "url_annotation_continuation": "accepted" if citations else "not_observed",
                      "citation_url_in_native_sources": all(citation["url"] in {source["url"] for source in sources} for citation in citations) if citations else None,
                      "citation_url_in_native_sources_without_tracking": all(without_tracking(citation["url"]) in {without_tracking(source["url"]) for source in sources} for citation in citations) if citations else None,
                      "native_source_url_recalled": any(s["url"] in text for s in sources),
                      "search_metrics": first, "recall_metrics": second}))


if __name__ == "__main__":
    main()
