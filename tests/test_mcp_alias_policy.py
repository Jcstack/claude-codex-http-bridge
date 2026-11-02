"""Security and compatibility properties of the experimental name policy."""

import itertools
import unittest

from mcp_alias_policy import AliasPolicy, ProtocolError, alias_for


def request(ident, method, params=None):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return message


def response(ident, result):
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def tool(name):
    return {"name": name, "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True}, "_meta": {"fixture": "kept"}}


class AliasMappingTest(unittest.TestCase):
    def test_injective_across_safe_unsafe_unicode_and_reserved_names(self):
        alphabet = "aA0_.- 中"
        originals = {"".join(chars) for size in range(1, 5)
                     for chars in itertools.product(alphabet, repeat=size)}
        originals.update({"zZ0_", "zZ0_ME", "a.b", "a_b", "A", "a", "\U0001f680"})
        aliases = [alias_for(value, "gateway") for value in originals]
        self.assertEqual(len(set(aliases)), len(originals))
        for value in aliases:
            self.assertRegex(value, r"\A[A-Za-z0-9_-]+\Z")

    def test_safe_names_preserved_and_reserved_names_encoded(self):
        self.assertEqual(alias_for("lookup_fixture-2", "gateway"), "lookup_fixture-2")
        self.assertEqual(alias_for("lookup.fixture", "gateway"), "zZ0_lookup_dfixture")
        self.assertNotEqual(alias_for("zZ0_ME", "gateway"), "zZ0_ME")
        self.assertEqual(alias_for("lookup.fixture", "gateway"), alias_for("lookup.fixture", "gateway"))

    def test_length_and_invalid_input_fail_closed(self):
        # Five characters in mcp__, two in __, and seven in gateway.
        self.assertEqual(len(alias_for("x" * 114, "gateway")), 114)
        for name, server in [("x" * 115, "gateway"), ("", "gateway"),
                             ("test", "bad.server"), ("\ud800", "gateway"),
                             ("a" * 4097, "gateway")]:
            with self.subTest(name=name[:10], server=server), self.assertRaises(ProtocolError):
                alias_for(name, server)


class ProtocolPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = AliasPolicy("gateway")

    def list_tools(self, names, ident=1, cursor=None):
        params = {"cursor": cursor} if cursor is not None else {}
        destination, _ = self.policy.client_message(request(ident, "tools/list", params))
        self.assertEqual(destination, "server")
        return self.policy.server_message(response(ident, {"tools": [tool(name) for name in names]}))[1]

    def test_distinct_mapping_and_exact_calls_preserve_metadata(self):
        listed = self.list_tools(["lookup.fixture", "lookup_fixture"])
        tools = listed["result"]["tools"]
        aliases = [item["name"] for item in tools]
        self.assertEqual(len(set(aliases)), 2)
        self.assertEqual(tools[0]["annotations"], {"readOnlyHint": True})
        self.assertEqual(tools[0]["_meta"], {"fixture": "kept"})
        for index, (alias, original) in enumerate(zip(aliases, ["lookup.fixture", "lookup_fixture"]), 2):
            sent = request(index, "tools/call", {"name": alias, "arguments": {"v": index}, "_meta": {"x": 1}})
            dest, forwarded = self.policy.client_message(sent)
            self.assertEqual(dest, "server")
            self.assertEqual(forwarded["params"], {**sent["params"], "name": original})
            self.assertEqual(self.policy.server_message(response(index, {"content": []}))[1]["result"], {"content": []})

    def test_unadvertised_call_never_goes_to_server(self):
        self.list_tools(["one"])
        for candidate in ["unknown", alias_for("hidden.tool", "gateway"), ""]:
            dest, reply = self.policy.client_message(request(2, "tools/call", {"name": candidate}))
            self.assertEqual(dest, "client")
            self.assertIn("error", reply)

    def test_paginated_tools_do_not_rebind_earlier_alias(self):
        first = self.list_tools(["a.b"], ident=1)
        second = self.list_tools(["a_b"], ident=2, cursor="page-2")
        for i, original, listed in [(3, "a.b", first), (4, "a_b", second)]:
            alias = listed["result"]["tools"][0]["name"]
            dest, sent = self.policy.client_message(request(i, "tools/call", {"name": alias}))
            self.assertEqual((dest, sent["params"]["name"]), ("server", original))

    def test_list_changed_invalidates_and_rejects_stale_list_response(self):
        old = self.list_tools(["old.tool"])["result"]["tools"][0]["name"]
        self.policy.client_message(request(2, "tools/list", {"cursor": "next"}))
        notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        self.assertEqual(self.policy.server_message(notification), ("client", notification))
        self.assertEqual(self.policy.client_message(request(3, "tools/call", {"name": old}))[0], "client")
        stale = self.policy.server_message(response(2, {"tools": [tool("stale")]}))[1]
        self.assertIn("error", stale)
        fresh = self.list_tools(["old.tool", "new.tool"], ident=4)
        self.assertEqual(fresh["result"]["tools"][0]["name"], old)
        self.assertEqual(self.policy.client_message(request(5, "tools/call", {"name": old}))[0], "server")

    def test_overlapping_refresh_invalidates_older_generation(self):
        self.policy.client_message(request(1, "tools/list"))
        self.policy.client_message(request(2, "tools/list"))
        self.assertIn("error", self.policy.server_message(response(1, {"tools": [tool("old")]}))[1])
        self.assertIn("result", self.policy.server_message(response(2, {"tools": [tool("new")]}))[1])
        self.assertEqual(self.policy.client_message(request(3, "tools/call", {"name": "old"}))[0], "client")
        self.assertEqual(self.policy.client_message(request(4, "tools/call", {"name": "new"}))[0], "server")

    def test_unmappable_page_rejects_whole_page_and_prior_aliases(self):
        self.list_tools(["prior"])
        self.policy.client_message(request(2, "tools/list", {"cursor": "next"}))
        reply = self.policy.server_message(response(2, {"tools": [tool("ok"), tool("x" * 115)]}))[1]
        self.assertIn("error", reply)
        for alias in ("prior", "ok"):
            self.assertEqual(self.policy.client_message(request(3, "tools/call", {"name": alias}))[0], "client")

    def test_malformed_list_and_mixed_result_error_rejected(self):
        for result in [{"tools": [None]}, {"tools": "wrong"}, {}]:
            with self.subTest(result=result):
                policy = AliasPolicy("gateway")
                policy.client_message(request(1, "tools/list"))
                self.assertIn("error", policy.server_message(response(1, result))[1])
        self.policy.client_message(request(1, "tools/list"))
        self.assertIn("error", self.policy.server_message({**response(1, {"tools": [tool("x")]}),
                                                          "error": {"code": -1}})[1])
        self.assertEqual(self.policy.advertised, {})

    def test_invalid_or_duplicate_ids_and_tool_notifications_fail_closed(self):
        self.policy.client_message(request(1, "resources/read"))
        with self.assertRaises(ProtocolError):
            self.policy.client_message(request(1, "tools/list"))
        for message in [request(True, "tools/list"), request(None, "tools/call"),
                        {"jsonrpc": "2.0", "method": "tools/call"}, []]:
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                self.policy.client_message(message)
        with self.assertRaises(ProtocolError):
            self.policy.server_message(response("unrecognized", {}))

    def test_server_request_and_client_response_use_separate_id_spaces(self):
        self.policy.client_message(request(1, "tools/list"))
        sampling = request(1, "sampling/createMessage", {"messages": []})
        self.assertEqual(self.policy.server_message(sampling), ("client", sampling))
        self.assertEqual(self.policy.client_message(response(1, {"content": "ok"}))[0], "server")
        self.assertIn("result", self.policy.server_message(response(1, {"tools": [tool("x")]}))[1])

    def test_non_tool_methods_and_cancellation_pass_through(self):
        for message in [request("read", "resources/read", {"uri": "fixture://x"}),
                        {"jsonrpc": "2.0", "method": "notifications/cancelled",
                         "params": {"requestId": "read"}}]:
            self.assertEqual(self.policy.client_message(message), ("server", message))
        result = response("read", {"contents": [{"uri": "fixture://x", "text": "ok"}]})
        self.assertEqual(self.policy.server_message(result), ("drop", None))
        with self.assertRaises(ProtocolError):
            self.policy.client_message(request("read", "resources/read"))

    def test_cancelled_list_invalidates_aliases_and_late_results_are_dropped(self):
        self.list_tools(["old.tool"])
        self.policy.client_message(request(2, "tools/list", {"cursor": "next"}))
        cancelled = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}
        self.assertEqual(self.policy.client_message(cancelled), ("server", cancelled))
        self.assertEqual(self.policy.advertised, {})
        self.assertEqual(self.policy.server_message(response(2, {"tools": [tool("raw.tool")]})), ("drop", None))
        self.assertEqual(self.policy.client_message(request(3, "tools/call", {"name": "raw_tool"}))[0], "client")
        self.assertIn("result", self.list_tools(["new.tool"], ident=4))

    def test_tool_retry_preserves_new_protocol_metadata_and_input_responses(self):
        alias = self.list_tools(["new.tool"])["result"]["tools"][0]["name"]
        params = {"name": alias, "arguments": {}, "inputResponses": {"consent": {"action": "accept"}},
                  "requestState": "synthetic-state", "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        dest, sent = self.policy.client_message(request(2, "tools/call", params))
        self.assertEqual(dest, "server")
        self.assertEqual(sent["params"], {**params, "name": "new.tool"})


if __name__ == "__main__":
    unittest.main()
