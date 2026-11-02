// Copied into a disposable pinned executor package by the browser-state gate.
// Synthetic credentials and a loopback fake only; not a real subscription test.
package executor

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	_ "github.com/router-for-me/CLIProxyAPI/v8/internal/translator/codex/claude"
	auth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	ex "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	tr "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestCodexBridgeNativeBrowserStateCandidate(t *testing.T) {
	requests := make(chan []byte, 4)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "fixture read failed", http.StatusBadRequest)
			return
		}
		requests <- body
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_fixture\",\"status\":\"completed\",\"model\":\"gpt-6.1-sol\",\"output\":[{\"type\":\"message\",\"role\":\"assistant\",\"status\":\"completed\",\"content\":[{\"type\":\"output_text\",\"text\":\"OK\"}]}],\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}}\n\n")
	}))
	defer server.Close()
	proxy := NewCodexExecutor(&config.Config{})
	credential := &auth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL},
		Metadata: map[string]any{"type": "codex", "access_token": "synthetic-access-token", "account_id": "synthetic-account"}}
	raw := []byte(`{"model":"gpt-6.1-sol","max_tokens":100,"messages":[{"role":"user","content":"fixture"},{"role":"assistant","content":[{"type":"tool_use","id":"mixed","name":"navigate","toolset_name":"browser","input":{}},{"type":"tool_use","id":"only","name":"list_tabs","toolset_name":"browser","input":{}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"mixed","toolset_name":"browser","content":[{"type":"text","text":"SYNTHETIC_BEFORE"},{"type":"browser_state","tabs":[{"tab_id":"SYNTHETIC_MIXED","title":"Fixture","url":"https://example.invalid","active":true}]}]},{"type":"tool_result","tool_use_id":"only","toolset_name":"browser","content":[{"type":"browser_state","tabs":[{"tab_id":"SYNTHETIC_ONLY","title":"Fixture","url":"https://example.invalid","active":true}]}]}]}],"tools":[{"type":"browser_toolset_20260801"}]}`)
	for _, streaming := range []bool{false, true} {
		req := ex.Request{Model: "gpt-6.1-sol", Payload: raw}
		options := ex.Options{SourceFormat: tr.FormatClaude, OriginalRequest: raw}
		var err error
		if streaming {
			var result *ex.StreamResult
			result, err = proxy.ExecuteStream(context.Background(), credential, req, options)
			if err == nil && result != nil {
				for range result.Chunks {
				}
			}
		} else {
			_, err = proxy.Execute(context.Background(), credential, req, options)
		}
		if err != nil {
			t.Fatal("synthetic native browser-state request failed")
		}
		body := <-requests
		outputs := map[string]gjson.Result{}
		for _, item := range gjson.GetBytes(body, "input").Array() {
			if item.Get("type").String() == "function_call_output" {
				outputs[item.Get("call_id").String()] = item.Get("output")
			}
		}
		mixed, only := outputs["mixed"].Array(), outputs["only"].Array()
		if len(mixed) != 2 || mixed[0].Get("text").String() != "SYNTHETIC_BEFORE" ||
			gjson.Get(mixed[1].Get("text").String(), "tabs.0.tab_id").String() != "SYNTHETIC_MIXED" ||
			len(only) != 1 || gjson.Get(only[0].Get("text").String(), "tabs.0.tab_id").String() != "SYNTHETIC_ONLY" {
			t.Errorf("synthetic browser state was not preserved for native executor (streaming=%v)", streaming)
		}
	}
}
