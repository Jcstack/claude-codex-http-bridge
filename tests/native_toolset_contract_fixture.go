// Copied into a disposable pinned translator package by an observation gate.
// Only synthetic Messages/Responses data is used, never a real model or tool.
package claude

import (
	"context"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
)

func TestSyntheticAnthropicClientToolsetsObservation(t *testing.T) {
	request := []byte(`{"model":"fixture","messages":[{"role":"user","content":"fixture"},{"role":"assistant","content":[{"type":"tool_use","id":"call_fixture","name":"navigate","toolset_name":"browser","input":{"url":"https://example.invalid"}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_fixture","toolset_name":"browser","content":[{"type":"text","text":"SYNTHETIC_NAV_DONE"},{"type":"browser_state","tabs":[{"tab_id":"SYNTHETIC_TAB_STATE","title":"Synthetic tab","url":"https://example.invalid","active":true}]}]}]}],"tools":[{"type":"browser_toolset_20260801","allowed_callers":["direct"],"configs":{"javascript_exec":{"enabled":true}}},{"type":"computer_toolset_20260801","allowed_callers":["direct"]},{"name":"navigate","input_schema":{"type":"object","properties":{}}}]}`)
	converted, err := ConvertClaudeRequestToCodex("fixture", request, false)
	if err != nil {
		t.Fatal("synthetic toolset request was rejected")
	}
	tools := gjson.GetBytes(converted, "tools").Array()
	if len(tools) != 3 || tools[0].Get("type").String() != "function" || tools[1].Get("type").String() != "function" ||
		tools[0].Get("name").String() != "" || tools[1].Get("name").String() != "" || tools[2].Get("name").String() != "navigate" {
		t.Fatal("synthetic toolset declaration observation changed")
	}
	if strings.Contains(string(converted), "SYNTHETIC_TAB_STATE") || !strings.Contains(string(converted), "SYNTHETIC_NAV_DONE") {
		t.Fatal("synthetic browser-state observation changed")
	}
	items := gjson.GetBytes(converted, "input").Array()
	callFound := false
	for _, item := range items {
		if item.Get("type").String() == "function_call" && item.Get("name").String() == "navigate" {
			callFound = true
			if item.Get("toolset_name").Exists() {
				t.Fatal("synthetic replay unexpectedly retained toolset identity")
			}
		}
	}
	if !callFound {
		t.Fatal("synthetic replay control did not include the function call")
	}
	terminal := []byte(`{"type":"response.completed","response":{"id":"resp_fixture","model":"fixture","status":"completed","output":[{"id":"item_fixture","type":"function_call","status":"completed","call_id":"call_fixture","name":"navigate","arguments":"{\"url\":\"https://example.invalid\"}"}],"usage":{"input_tokens":1,"output_tokens":1}}}`)
	result := ConvertCodexResponseToClaudeNonStream(context.Background(), "fixture", request, nil, terminal, nil)
	blocks := gjson.GetBytes(result, "content").Array()
	if len(blocks) != 1 || blocks[0].Get("type").String() != "tool_use" || blocks[0].Get("name").String() != "navigate" || blocks[0].Get("toolset_name").Exists() {
		t.Fatal("synthetic nonstreaming toolset identity observation changed")
	}
	var state any
	events := []string{
		`data: {"type":"response.created","response":{"id":"resp_fixture","model":"fixture"}}`,
		`data: {"type":"response.output_item.added","output_index":0,"item":{"id":"item_fixture","type":"function_call","call_id":"call_fixture","name":"navigate","arguments":""}}`,
		`data: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"item_fixture","delta":"{\"url\":\"https://example.invalid\"}"}`,
		`data: {"type":"response.output_item.done","output_index":0,"item":{"id":"item_fixture","type":"function_call","status":"completed","call_id":"call_fixture","name":"navigate","arguments":"{\"url\":\"https://example.invalid\"}"}}`,
		"data: " + string(terminal),
	}
	var streamed strings.Builder
	for _, event := range events {
		for _, chunk := range ConvertCodexResponseToClaude(context.Background(), "fixture", request, nil, []byte(event), &state) {
			streamed.Write(chunk)
		}
	}
	if !strings.Contains(streamed.String(), `"name":"navigate"`) || strings.Contains(streamed.String(), `"toolset_name"`) {
		t.Fatal("synthetic streaming toolset identity observation changed")
	}
	t.Log("synthetic browser/computer toolsets were not expanded, browser-state content was omitted, and toolset identity was not retained")
}
