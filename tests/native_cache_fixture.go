// This fixture is copied into the pinned upstream executor package by CI.
// It uses synthetic OAuth-shaped metadata and an in-process loopback server.
package executor

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor/helps"
	_ "github.com/router-for-me/CLIProxyAPI/v8/internal/translator/codex/claude"
	auth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	ex "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	tr "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestCodexBridgeNativeDynamicToolCachePrefix(t *testing.T) {
	requests := make(chan []byte, 8)
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
	tool := func(name string, deferred bool) map[string]any {
		value := map[string]any{"name": name, "description": "Synthetic fixture", "input_schema": map[string]any{
			"type": "object", "properties": map[string]any{"value": map[string]any{"type": "string"}}}}
		if deferred {
			value["defer_loading"] = true
		}
		return value
	}
	fixture := func(session, model, effort, phase string) []byte {
		messages := []any{map[string]any{"role": "user", "content": "Search synthetic tools"}}
		added := strings.HasPrefix(phase, "added-")
		selected := "mcp__demo__lookup"
		if added {
			selected = "mcp__demo__extra"
		}
		if phase == "revealed" || phase == "removed" || phase == "added-revealed" || phase == "added-removed" {
			messages = append(messages,
				map[string]any{"role": "assistant", "content": []any{map[string]any{"type": "tool_use", "id": "toolu_search", "name": "ToolSearch", "input": map[string]any{"query": selected}}}},
				map[string]any{"role": "user", "content": []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_search", "content": []any{map[string]any{"type": "tool_reference", "tool_name": selected}}}}})
		}
		if phase == "removed" || phase == "added-removed" {
			messages = append(messages, map[string]any{"role": "system", "content": []any{map[string]any{"type": "tool_removal", "tool": map[string]any{"type": "tool_reference", "name": selected}}}})
		}
		tools := []any{tool("ToolSearch", false), tool("Read", false), tool("mcp__demo__lookup", true)}
		if added {
			tools = append(tools, tool("mcp__demo__extra", true))
		}
		metadata, _ := json.Marshal(map[string]string{"session_id": session})
		body, err := json.Marshal(map[string]any{"model": model, "max_tokens": 1024, "system": "Synthetic stable prefix.",
			"metadata": map[string]any{"user_id": string(metadata)}, "thinking": map[string]any{"type": "adaptive"},
			"output_config": map[string]any{"effort": effort}, "messages": messages,
			"tools": tools})
		if err != nil {
			t.Fatal("fixture serialization failed")
		}
		return body
	}
	send := func(session, model, effort, phase, agent string) []byte {
		raw := fixture(session, model, effort, phase)
		headers := http.Header{}
		if agent != "" {
			headers.Set(helps.ClaudeCodeAgentHeader, agent)
		}
		_, err := proxy.Execute(context.Background(), credential, ex.Request{Model: model, Payload: raw},
			ex.Options{SourceFormat: tr.FormatClaude, OriginalRequest: raw, Headers: headers})
		if err != nil {
			t.Fatal("synthetic native executor request failed")
		}
		return <-requests
	}
	const sessionA = "00000000-0000-4000-8000-000000000001"
	const sessionB = "00000000-0000-4000-8000-000000000002"
	const sol = "gpt-6.1-sol"
	before := send(sessionA, sol, "max", "before", "")
	revealed := send(sessionA, sol, "max", "revealed", "")
	removed := send(sessionA, sol, "max", "removed", "")
	otherSession := send(sessionB, sol, "max", "before", "")
	otherAgent := send(sessionA, sol, "max", "before", "child-fixture")
	otherModel := send(sessionA, "gpt-6.1-terra", "max", "before", "")
	otherEffort := send(sessionA, sol, "high", "before", "")
	addedBefore := send(sessionA, sol, "max", "added-before", "")
	addedRevealed := send(sessionA, sol, "max", "added-revealed", "")
	addedRemoved := send(sessionA, sol, "max", "added-removed", "")

	for _, field := range []string{"instructions", "tools"} {
		first := gjson.GetBytes(before, field)
		if !first.Exists() || first.Raw != gjson.GetBytes(revealed, field).Raw || first.Raw != gjson.GetBytes(removed, field).Raw {
			t.Fatalf("native fixture changed the %s prefix", field)
		}
	}
	prefix := func(first, next []byte) bool {
		old := gjson.GetBytes(first, "input").Array()
		newer := gjson.GetBytes(next, "input").Array()
		if len(old) == 0 || len(old) > len(newer) {
			return false
		}
		for index := range old {
			if old[index].Raw != newer[index].Raw {
				return false
			}
		}
		return true
	}
	if !prefix(before, revealed) || !prefix(revealed, removed) ||
		!strings.Contains(gjson.GetBytes(before, "input.0").Raw, "Synthetic stable prefix.") {
		t.Fatal("native fixture did not preserve the system and input item prefix")
	}
	toolNames := func(body []byte) map[string]bool {
		result := map[string]bool{}
		for _, tool := range gjson.GetBytes(body, "tools").Array() {
			result[tool.Get("name").String()] = true
		}
		return result
	}
	inlineNames := func(body []byte) map[string]bool {
		result := map[string]bool{}
		for _, item := range gjson.GetBytes(body, "input").Array() {
			if item.Get("type").String() != "additional_tools" {
				continue
			}
			for _, tool := range item.Get("tools").Array() {
				result[tool.Get("name").String()] = true
			}
		}
		return result
	}
	choice := gjson.GetBytes(removed, "tool_choice")
	allowed := map[string]bool{}
	for _, tool := range choice.Get("tools").Array() {
		allowed[tool.Get("name").String()] = true
	}
	if toolNames(before)["mcp__demo__lookup"] || !inlineNames(revealed)["mcp__demo__lookup"] ||
		!inlineNames(removed)["mcp__demo__lookup"] || choice.Get("type").String() != "allowed_tools" ||
		allowed["mcp__demo__lookup"] || !allowed["ToolSearch"] || !allowed["Read"] {
		t.Fatal("native fixture did not preserve dynamic tool semantics")
	}
	for _, field := range []string{"instructions", "tools"} {
		first := gjson.GetBytes(before, field)
		if !first.Exists() || first.Raw != gjson.GetBytes(addedBefore, field).Raw ||
			first.Raw != gjson.GetBytes(addedRevealed, field).Raw || first.Raw != gjson.GetBytes(addedRemoved, field).Raw {
			t.Fatalf("added native tool changed the %s prefix", field)
		}
	}
	addedChoice := gjson.GetBytes(addedRemoved, "tool_choice")
	addedAllowed := map[string]bool{}
	for _, tool := range addedChoice.Get("tools").Array() {
		addedAllowed[tool.Get("name").String()] = true
	}
	if !prefix(before, addedBefore) || !prefix(addedBefore, addedRevealed) || !prefix(addedRevealed, addedRemoved) ||
		toolNames(addedBefore)["mcp__demo__extra"] || inlineNames(addedBefore)["mcp__demo__extra"] ||
		!inlineNames(addedRevealed)["mcp__demo__extra"] || !inlineNames(addedRemoved)["mcp__demo__extra"] ||
		addedChoice.Get("type").String() != "allowed_tools" || addedAllowed["mcp__demo__extra"] ||
		!addedAllowed["ToolSearch"] || !addedAllowed["Read"] {
		t.Fatal("native fixture did not preserve newly added deferred tool semantics")
	}
	for _, field := range []string{"model", "instructions", "tools", "input"} {
		original := gjson.GetBytes(before, field)
		if !original.Exists() || original.Raw != gjson.GetBytes(otherSession, field).Raw {
			t.Fatalf("other-session fixture changed %s", field)
		}
	}
	key := func(body []byte) string { return gjson.GetBytes(body, "prompt_cache_key").String() }
	if key(before) == "" || key(before) != key(revealed) || key(before) != key(removed) ||
		key(before) != key(addedBefore) || key(before) != key(addedRevealed) || key(before) != key(addedRemoved) ||
		key(before) != key(otherEffort) || key(before) == key(otherSession) ||
		key(before) == key(otherAgent) || key(before) == key(otherModel) ||
		key(otherSession) == "" || key(otherAgent) == "" || key(otherModel) == "" {
		t.Fatal("native fixture cache-key scope did not match session, agent and model")
	}
	if gjson.GetBytes(before, "reasoning.effort").String() != "max" ||
		gjson.GetBytes(otherEffort, "reasoning.effort").String() != "high" {
		t.Fatal("native fixture did not preserve the selected effort")
	}
}

func TestCodexBridgeResponsesCacheKeyReachesNativeRequest(t *testing.T) {
	proxy := NewCodexExecutor(&config.Config{})
	for _, cacheKey := range []string{"synthetic-session-a", "synthetic-session-b"} {
		payload, err := json.Marshal(map[string]any{"model": "gpt-6.1-sol", "stream": true,
			"prompt_cache_key": cacheKey, "input": []any{map[string]any{"role": "user", "content": "fixture"}}})
		if err != nil {
			t.Fatal("synthetic Responses serialization failed")
		}
		request, body, err := proxy.cacheHelper(context.Background(), tr.FormatOpenAIResponse,
			"https://example.invalid/v1/responses", ex.Request{Model: "gpt-6.1-sol", Payload: payload}, payload, nil)
		if err != nil {
			t.Fatal("synthetic Responses cache setup failed")
		}
		if gjson.GetBytes(body, "prompt_cache_key").String() != cacheKey || request.Header.Get("Session-Id") != cacheKey {
			t.Fatal("Responses cache key was not preserved for the native upstream request")
		}
	}
}
