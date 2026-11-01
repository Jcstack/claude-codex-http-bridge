// Copied into the pinned Codex executor package by the credential-free gate.
package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	_ "github.com/router-for-me/CLIProxyAPI/v8/internal/translator/codex/claude"
	auth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	ex "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	tr "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestCodexBridgeIncompleteToolNonStream(t *testing.T) {
	for _, tc := range []struct {
		name, terminal, status   string
		includeStatus, wantError bool
	}{
		{"incomplete_in_progress", "response.incomplete", "in_progress", true, true},
		{"incomplete_incomplete", "response.incomplete", "incomplete", true, true},
		{"incomplete_unknown", "response.incomplete", "", false, true},
		{"incomplete_completed", "response.incomplete", "completed", true, false},
		{"completed_in_progress", "response.completed", "in_progress", true, true},
		{"completed_unknown", "response.completed", "", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			item := map[string]any{"type": "function_call", "id": "item_fixture", "call_id": "call_fixture",
				"name": "Read", "arguments": `{"file_path":"synthetic"}`}
			if tc.includeStatus {
				item["status"] = tc.status
			}
			status := "incomplete"
			if tc.terminal == "response.completed" {
				status = "completed"
			}
			response := map[string]any{"id": "resp_fixture", "model": "gpt-6.1-sol", "status": status,
				"output": []any{item}, "usage": map[string]any{"input_tokens": 10, "output_tokens": 5}}
			if status == "incomplete" {
				response["incomplete_details"] = map[string]any{"reason": "max_output_tokens"}
			}
			event, err := json.Marshal(map[string]any{"type": tc.terminal, "response": response})
			if err != nil {
				t.Fatal(err)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = w.Write(append(append([]byte("data: "), event...), '\n', '\n'))
			}))
			defer server.Close()
			proxy := NewCodexExecutor(&config.Config{})
			credential := &auth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL, "api_key": "synthetic"}}
			raw := []byte(`{"model":"gpt-6.1-sol","max_tokens":100,"messages":[{"role":"user","content":"fixture"}],"tools":[{"name":"Read","input_schema":{"type":"object"}}]}`)
			result, err := proxy.Execute(context.Background(), credential, ex.Request{Model: "gpt-6.1-sol", Payload: raw},
				ex.Options{SourceFormat: tr.FormatClaude, OriginalRequest: raw})
			if tc.wantError {
				if err == nil {
					t.Fatal("unconfirmed tool was accepted")
				}
				status, ok := err.(interface{ StatusCode() int })
				if !ok || status.StatusCode() != http.StatusBadGateway {
					t.Fatal("missing upstream failure status")
				}
				scope, ok := err.(interface{ IsRequestScoped() bool })
				if !ok || !scope.IsRequestScoped() {
					t.Fatal("tool failure could cool down the account")
				}
				return
			}
			if err != nil {
				t.Fatalf("confirmed fixture failed: %v", err)
			}
			if gjson.GetBytes(result.Payload, "stop_reason").String() != "tool_use" ||
				gjson.GetBytes(result.Payload, "content.0.name").String() != "Read" {
				t.Fatal("confirmed tool did not survive non-streaming translation")
			}
		})
	}
}

func TestCodexBridgeIncompleteToolWebsocket(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		for _, unfinished := range []bool{false, true} {
			name := "completed"
			if unfinished {
				name = "unfinished"
			}
			if streaming {
				name += "_stream"
			} else {
				name += "_nonstream"
			}
			t.Run(name, func(t *testing.T) {
				upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					conn, err := upgrader.Upgrade(w, r, nil)
					if err != nil {
						return
					}
					defer conn.Close()
					if _, _, err = conn.ReadMessage(); err != nil {
						return
					}
					status, terminal := "completed", "response.completed"
					if unfinished {
						status, terminal = "in_progress", "response.incomplete"
					}
					item := map[string]any{"type": "function_call", "id": "item_fixture", "call_id": "call_fixture",
						"name": "Read", "arguments": `{"file_path":"synthetic"}`, "status": status}
					events := []map[string]any{
						{"type": "response.created", "response": map[string]any{"id": "resp_fixture", "model": "gpt-6.1-sol"}},
						{"type": "response.output_item.added", "output_index": 0,
							"item": map[string]any{"type": "function_call", "id": "item_fixture", "call_id": "call_fixture", "name": "Read", "status": "in_progress"}},
						{"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "item_fixture", "arguments": item["arguments"]},
						{"type": terminal, "response": map[string]any{"id": "resp_fixture", "model": "gpt-6.1-sol",
							"output": []any{item}, "usage": map[string]any{"input_tokens": 10, "output_tokens": 5}}},
					}
					for _, event := range events {
						payload, _ := json.Marshal(event)
						if conn.WriteMessage(websocket.TextMessage, payload) != nil {
							return
						}
					}
				}))
				defer server.Close()
				proxy := NewCodexWebsocketsExecutor(&config.Config{})
				credential := &auth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL, "api_key": "synthetic"}}
				raw := []byte(`{"model":"gpt-6.1-sol","max_tokens":100,"messages":[{"role":"user","content":"fixture"}],"tools":[{"name":"Read","input_schema":{"type":"object"}}]}`)
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				request := ex.Request{Model: "gpt-6.1-sol", Payload: raw}
				opts := ex.Options{SourceFormat: tr.FormatClaude, OriginalRequest: raw, Stream: streaming}
				if !streaming {
					result, err := proxy.Execute(ctx, credential, request, opts)
					if unfinished {
						if err == nil {
							t.Fatal("unfinished WebSocket tool was accepted")
						}
						scope, ok := err.(interface{ IsRequestScoped() bool })
						if !ok || !scope.IsRequestScoped() {
							t.Fatal("WebSocket failure could cool the account")
						}
					} else if err != nil || gjson.GetBytes(result.Payload, "stop_reason").String() != "tool_use" {
						t.Fatal("completed WebSocket tool was not preserved")
					}
					return
				}
				result, err := proxy.ExecuteStream(ctx, credential, request, opts)
				if err != nil {
					t.Fatalf("WebSocket fixture failed before output: %v", err)
				}
				var output []byte
				for chunk := range result.Chunks {
					output = append(output, chunk.Payload...)
				}
				if unfinished {
					if !bytes.Contains(output, []byte(`"type":"error"`)) || bytes.Contains(output, []byte(`"type":"message_stop"`)) || bytes.Contains(output, []byte(`"type":"content_block_stop"`)) {
						t.Fatal("unfinished WebSocket tool was falsely completed")
					}
				} else if !bytes.Contains(output, []byte(`"type":"message_stop"`)) || !bytes.Contains(output, []byte(`"type":"tool_use"`)) {
					t.Fatal("completed WebSocket stream was not preserved")
				}
			})
		}
	}
}
