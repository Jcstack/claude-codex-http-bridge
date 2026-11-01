// Copied into the pinned Claude translator package by the credential-free gate.
package claude

import (
	"bytes"
	"context"
	"testing"
)

func bridgeIncompleteEvents(events ...string) []byte {
	var state any
	var result []byte
	request := []byte(`{"tools":[{"name":"Read","input_schema":{"type":"object"}}]}`)
	for _, event := range events {
		for _, part := range ConvertCodexResponseToClaude(context.Background(), "gpt-6.1-sol", request, nil, []byte("data: "+event), &state) {
			result = append(result, part...)
		}
	}
	return result
}

func TestBridgeIncompleteToolErrorIsTerminal(t *testing.T) {
	out := bridgeIncompleteEvents(
		`{"type":"response.created","response":{"id":"fixture","model":"gpt-6.1-sol"}}`,
		`{"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","status":"in_progress"}}`,
		`{"type":"response.function_call_arguments.done","output_index":0,"item_id":"item_fixture","arguments":"{}"}`,
		`{"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","arguments":"{}","status":"incomplete"}}`,
		`{"type":"response.completed","response":{"output":[{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","arguments":"{}","status":"completed"}]}}`,
	)
	if bytes.Count(out, []byte(`"type":"error"`)) != 1 || bytes.Contains(out, []byte(`"type":"content_block_stop"`)) || bytes.Contains(out, []byte(`"type":"message_stop"`)) {
		t.Fatal("an error was followed by a fabricated completed tool or message")
	}
}

func TestBridgeIncompleteTextRetainsMaxTokens(t *testing.T) {
	out := bridgeIncompleteEvents(
		`{"type":"response.created","response":{"id":"fixture","model":"gpt-6.1-sol"}}`,
		`{"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"partial"}`,
		`{"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"},"output":[]}}`,
	)
	if !bytes.Contains(out, []byte(`"stop_reason":"max_tokens"`)) || !bytes.Contains(out, []byte(`"type":"message_stop"`)) || bytes.Contains(out, []byte(`"type":"error"`)) {
		t.Fatal("ordinary text truncation did not remain a max_tokens response")
	}
}

func TestBridgeIncompleteUnknownToolStatusIsRejected(t *testing.T) {
	out := bridgeIncompleteEvents(
		`{"type":"response.created","response":{"id":"fixture","model":"gpt-6.1-sol"}}`,
		`{"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read"}}`,
		`{"type":"response.function_call_arguments.done","output_index":0,"item_id":"item_fixture","arguments":"{}"}`,
		`{"type":"response.incomplete","response":{"output":[{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","arguments":"{}"}]}}`,
	)
	if !bytes.Contains(out, []byte(`"type":"error"`)) || bytes.Contains(out, []byte(`"type":"message_stop"`)) {
		t.Fatal("unknown tool completion status was accepted")
	}
}

func TestBridgeIncompletePreviouslyCompletedToolRemainsAvailable(t *testing.T) {
	out := bridgeIncompleteEvents(
		`{"type":"response.created","response":{"id":"fixture","model":"gpt-6.1-sol"}}`,
		`{"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read"}}`,
		`{"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","arguments":"{}"}}`,
		`{"type":"response.incomplete","response":{"output":[{"type":"function_call","id":"item_fixture","call_id":"call_fixture","name":"Read","arguments":"{}"}]}}`,
	)
	if bytes.Contains(out, []byte(`"type":"error"`)) || bytes.Count(out, []byte(`"type":"tool_use"`)) != 1 || !bytes.Contains(out, []byte(`"type":"message_stop"`)) {
		t.Fatal("a previously confirmed tool was lost or duplicated")
	}
}
