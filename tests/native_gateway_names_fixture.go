// Integration checks for the upstream readable gateway-name backport. This is
// copied into the pinned translator package by the isolated candidate workflow.
package claude

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
)

func TestBridgeGatewayNamesSurviveDeferredToolLifecycle(t *testing.T) {
	first := "mcp__gateway__mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_create-branch"
	second := "mcp__gateway__mcp_github-0b9e7c1d-1111-2222-3333-444455556666-89abcdef_create-branch"
	tools := []any{
		map[string]any{"name": "ToolSearch", "input_schema": map[string]any{"type": "object"}},
		map[string]any{"name": first, "defer_loading": true, "input_schema": map[string]any{"type": "object"}},
		map[string]any{"name": second, "defer_loading": true, "input_schema": map[string]any{"type": "object"}},
	}
	initial := []any{map[string]any{"role": "user", "content": "Find a synthetic tool"}}
	revealed := append(append([]any{}, initial...),
		map[string]any{"role": "assistant", "content": []any{map[string]any{"type": "tool_use", "id": "search_1", "name": "ToolSearch", "input": map[string]any{}}}},
		map[string]any{"role": "user", "content": []any{map[string]any{"type": "tool_result", "tool_use_id": "search_1", "content": []any{
			map[string]any{"type": "tool_reference", "tool_name": first}, map[string]any{"type": "tool_reference", "tool_name": second},
		}}}},
	)
	makeRequest := func(messages []any) []byte {
		b, err := json.Marshal(map[string]any{"model": "gpt-6.1-sol", "tools": tools, "messages": messages})
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	translate := func(raw []byte) []byte {
		b, err := ConvertClaudeRequestToCodex("gpt-6.1-sol", raw, false)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	before := translate(makeRequest(initial))
	raw := makeRequest(revealed)
	after := translate(raw)
	if gjson.GetBytes(before, "tools").Raw != gjson.GetBytes(after, "tools").Raw {
		t.Fatal("deferred reveal changed the stable top-level tool prefix")
	}
	var names []string
	for _, item := range gjson.GetBytes(after, "input").Array() {
		if item.Get("type").String() == "additional_tools" {
			for _, tool := range item.Get("tools").Array() {
				names = append(names, tool.Get("name").String())
			}
		}
	}
	if len(names) != 2 || names[0] == names[1] || !strings.Contains(names[0], "create-branch") || !strings.Contains(names[1], "create-branch") || len(names[0]) > 64 || len(names[1]) > 64 {
		t.Fatalf("gateway names are not distinct, readable and bounded: %q", names)
	}
	if !strings.Contains(names[0], "12345678") || !strings.Contains(names[1], "0b9e7c1d") {
		t.Fatal("gateway names lost their recognizable instance prefixes")
	}
	completed := []byte(fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_fixture","model":"gpt-6.1-sol","output":[{"type":"function_call","call_id":"call_a","name":%q,"arguments":"{}"},{"type":"function_call","call_id":"call_b","name":%q,"arguments":"{}"}]}}`, names[0], names[1]))
	var nonstreamState any
	answer := ConvertCodexResponseToClaudeNonStream(context.Background(), "", raw, nil, completed, &nonstreamState)
	if gjson.GetBytes(answer, "content.0.name").String() != first || gjson.GetBytes(answer, "content.1.name").String() != second {
		t.Fatal("non-streaming tool names did not restore both original names")
	}
	var streamState any
	var stream []byte
	for _, event := range perfCodexEvents(names) {
		stream = append(stream, bytes.Join(ConvertCodexResponseToClaude(context.Background(), "", raw, nil, event, &streamState), nil)...)
	}
	for _, name := range []string{first, second} {
		if !bytes.Contains(stream, []byte(name)) {
			t.Fatal("streaming tool name did not restore its original")
		}
	}
	continued := append(append([]any{}, revealed...),
		map[string]any{"role": "assistant", "content": []any{
			map[string]any{"type": "tool_use", "id": "call_a", "name": first, "input": map[string]any{}},
			map[string]any{"type": "tool_use", "id": "call_b", "name": second, "input": map[string]any{}},
		}},
		map[string]any{"role": "user", "content": []any{
			map[string]any{"type": "tool_result", "tool_use_id": "call_a", "content": "a"},
			map[string]any{"type": "tool_result", "tool_use_id": "call_b", "content": "b"},
		}},
		map[string]any{"role": "system", "content": []any{map[string]any{"type": "tool_removal", "tool": map[string]any{"type": "tool_reference", "name": first}}}},
	)
	last := translate(makeRequest(continued))
	previous := gjson.GetBytes(after, "input").Array()
	now := gjson.GetBytes(last, "input").Array()
	if len(now) < len(previous) {
		t.Fatal("continuation discarded the earlier input")
	}
	for i := range previous {
		if previous[i].Raw != now[i].Raw {
			t.Fatal("continuation changed the cached input prefix")
		}
	}
	seen := map[string]bool{}
	for _, item := range now {
		if item.Get("type").String() == "function_call" {
			seen[item.Get("name").String()] = true
		}
	}
	if !seen[names[0]] || !seen[names[1]] {
		t.Fatal("continuation did not replay both mapped tool calls")
	}
	allowed := map[string]bool{}
	for _, item := range gjson.GetBytes(last, "tool_choice.tools").Array() {
		allowed[item.Get("name").String()] = true
	}
	if allowed[names[0]] || !allowed[names[1]] {
		t.Fatal("tool removal did not constrain the mapped names")
	}
}

func TestBridgeGatewayNameCollisionWithExistingShortName(t *testing.T) {
	first := "mcp__gateway__mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_create-branch"
	second := "mcp__gateway__mcp_github-0b9e7c1d-1111-2222-3333-444455556666-89abcdef_create-branch"
	short := shortenNameIfNeeded(first)
	names := buildShortNameMap([]string{short, first, second})
	if names[short] != short || names[first] == names[second] || names[first] == short || names[second] == short {
		t.Fatal("short names collided with an existing tool")
	}
	for _, name := range []string{first, second} {
		if !strings.Contains(names[name], "create-branch") || len(names[name]) > 64 {
			t.Fatal("collision suffix obscured the tool or exceeded the limit")
		}
	}
}

func TestBridgeGatewaySharedInstancePrefixesAreStillDistinct(t *testing.T) {
	first := "mcp__gateway__mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_create-branch"
	second := "mcp__gateway__mcp_github-12345678-1111-2222-3333-444455556666-89abcdef_create-branch"
	names := buildShortNameMap([]string{first, second})
	if names[first] == names[second] || !strings.Contains(names[first], "12345678-") || !strings.Contains(names[second], "12345678-") {
		t.Fatal("different instances sharing an ID prefix were not distinguished")
	}
	for _, name := range []string{first, second} {
		if !strings.Contains(names[name], "create-branch") || len(names[name]) > 64 {
			t.Fatal("instance identifier obscured the action or exceeded the limit")
		}
	}
}

func TestBridgeGatewayNameBudgetFallsBackExplicitly(t *testing.T) {
	prefix := "mcp__gateway__mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_"
	action := strings.Repeat("x", 39)
	short := shortenNameIfNeeded(prefix + action)
	if len(short) > 64 || !strings.Contains(short, "12345678") || !strings.HasSuffix(short, action) {
		t.Fatal("the compact identity fallback did not retain the instance and action")
	}
	longAction := strings.Repeat("x", 49)
	short = shortenNameIfNeeded(prefix + longAction)
	if len(short) > 64 || !strings.HasSuffix(short, longAction) || strings.Contains(short, "12345678") {
		t.Fatal("the final length-budget fallback changed unexpectedly")
	}
}
