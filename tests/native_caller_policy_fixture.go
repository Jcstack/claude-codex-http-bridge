// Copied into a disposable pinned executor package by the caller-policy gate.
// Synthetic credentials and a loopback fake only; not a real subscription test.
package executor

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	_ "github.com/router-for-me/CLIProxyAPI/v8/internal/translator/codex/claude"
	auth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	ex "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	tr "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestCodexBridgeNativeCallerPolicy(t *testing.T) {
	var calls atomic.Int32
	requests := make(chan []byte, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "fixture read failed", http.StatusBadRequest)
			return
		}
		calls.Add(1)
		requests <- body
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_fixture\",\"status\":\"completed\",\"model\":\"gpt-6.1-sol\",\"output\":[{\"type\":\"message\",\"role\":\"assistant\",\"status\":\"completed\",\"content\":[{\"type\":\"output_text\",\"text\":\"OK\"}]}],\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}}\n\n")
	}))
	defer server.Close()
	proxy := NewCodexExecutor(&config.Config{})
	credential := &auth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL},
		Metadata: map[string]any{"type": "codex", "access_token": "synthetic-access-token", "account_id": "synthetic-account"}}
	fixture := func(tool string) []byte {
		return []byte(`{"model":"gpt-6.1-sol","max_tokens":100,"messages":[{"role":"user","content":"fixture"}],"tools":[` + tool + `]}`)
	}
	const schema = `{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}`
	valid := fixture(`{"name":"fixture_tool","strict":true,"allowed_callers":["direct"],"input_schema":` + schema + `}`)
	request := ex.Request{Model: "gpt-6.1-sol", Payload: valid}
	opts := ex.Options{SourceFormat: tr.FormatClaude, OriginalRequest: valid}
	if _, err := proxy.Execute(context.Background(), credential, request, opts); err != nil {
		t.Fatal("direct caller native control failed")
	}
	first := <-requests
	if gjson.GetBytes(first, "tools.0.strict").Type != gjson.False || calls.Load() != 1 {
		t.Error("caller-only candidate changed the existing strict translation")
	}
	for _, streaming := range []bool{false, true} {
		for _, callers := range []string{`["code_execution_20260120"]`, `["direct","unknown_future"]`, `null`} {
			before := calls.Load()
			raw := fixture(`{"name":"fixture_tool","allowed_callers":` + callers + `,"input_schema":` + schema + `}`)
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
			status, statusOK := err.(interface{ StatusCode() int })
			scope, scopeOK := err.(interface{ IsRequestScoped() bool })
			if !statusOK || status.StatusCode() != 400 || !scopeOK || !scope.IsRequestScoped() || calls.Load() != before {
				t.Errorf("caller policy was not rejected locally without account cooldown (streaming=%v)", streaming)
			}
		}
	}
	before := calls.Load()
	if _, err := proxy.Execute(context.Background(), credential, request, opts); err != nil || calls.Load() != before+1 {
		t.Fatal("healthy native request failed after rejected fixtures")
	}
}
