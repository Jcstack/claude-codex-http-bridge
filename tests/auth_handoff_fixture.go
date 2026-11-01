// Copied into the pinned upstream sdk/cliproxy/auth package as an isolated test.
package auth

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	ex "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

type bridgeHandoffExecutor struct {
	customStreamMockExecutor
	closed []string
}

func (e *bridgeHandoffExecutor) CloseAuthExecutionSessions(id string) {
	e.closed = append(e.closed, id)
}

func TestBridgeAccountHandoffKeepsAdmittedStream(t *testing.T) {
	ctx := context.Background()
	selector := NewSessionAffinitySelector(&FillFirstSelector{})
	defer selector.Stop()
	manager := NewManager(nil, selector, nil)
	manager.SetRetryConfig(0, 0, 0)
	const model = "bridge-handoff-fixture-model"
	for _, id := range []string{"bridge-old", "bridge-new"} {
		registry.GetGlobalRegistry().RegisterClient(id, "codex", []*registry.ModelInfo{{ID: model}})
		defer registry.GetGlobalRegistry().UnregisterClient(id)
	}
	old := &Auth{ID: "bridge-old", Provider: "codex", Status: StatusActive}
	if _, err := manager.Register(ctx, old); err != nil {
		t.Fatal(err)
	}
	oldChunks := make(chan ex.StreamChunk, 2)
	oldChunks <- ex.StreamChunk{Payload: []byte("old:start")}
	var snapshot *Auth
	var oldContext context.Context
	executor := &bridgeHandoffExecutor{}
	executor.identifier = "codex"
	executor.streamFn = func(c context.Context, a *Auth, _ ex.Request, _ ex.Options) (*ex.StreamResult, error) {
		if a.ID == "bridge-old" {
			snapshot, oldContext = a, c
			return &ex.StreamResult{Chunks: oldChunks}, nil
		}
		chunks := make(chan ex.StreamChunk, 1)
		chunks <- ex.StreamChunk{Payload: []byte(a.ID)}
		close(chunks)
		return &ex.StreamResult{Chunks: chunks}, nil
	}
	manager.RegisterExecutor(executor)
	opts := ex.Options{Headers: http.Header{"X-Session-Id": []string{"same-running-session"}}}
	before, err := manager.ExecuteStream(ctx, []string{"codex"}, ex.Request{Model: model}, opts)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case chunk := <-before.Chunks:
		if string(chunk.Payload) != "old:start" {
			t.Fatalf("first chunk: %s", chunk.Payload)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("old stream did not start")
	}
	if _, err := manager.Register(ctx, &Auth{ID: "bridge-new", Provider: "codex", Status: StatusActive,
		Attributes: map[string]string{"priority": "10"}}); err != nil {
		t.Fatal(err)
	}
	_, committed, err := manager.Modify(ctx, "bridge-old", 0, func(a *Auth) error {
		a.Disabled, a.Status = true, StatusDisabled
		return nil
	})
	if err != nil || !committed {
		t.Fatalf("disable: %v", err)
	}
	after, err := manager.ExecuteStream(ctx, []string{"codex"}, ex.Request{Model: model}, opts)
	if err != nil {
		t.Fatal(err)
	}
	var next string
	for chunk := range after.Chunks {
		next += string(chunk.Payload)
	}
	if next != "bridge-new" {
		t.Fatalf("next request used %q", next)
	}
	if len(executor.closed) != 0 {
		t.Fatalf("disable closed admitted execution sessions")
	}
	if snapshot == nil || snapshot.Disabled || oldContext.Err() != nil {
		t.Fatal("old request snapshot/context changed")
	}
	oldChunks <- ex.StreamChunk{Payload: []byte("old:finished")}
	close(oldChunks)
	var rest string
	for chunk := range before.Chunks {
		rest += string(chunk.Payload)
	}
	if rest != "old:finished" {
		t.Fatalf("old output interrupted: %q", rest)
	}
	current, ok := manager.GetByID("bridge-old")
	if !ok || !current.Disabled {
		t.Fatal("late old result re-enabled the old account")
	}
}

type bridgePinQuotaError struct{}

func (bridgePinQuotaError) Error() string   { return "synthetic rate limit" }
func (bridgePinQuotaError) StatusCode() int { return http.StatusTooManyRequests }

// Exact pins are tested through the internal manager, not through any HTTP API.
func TestBridgeAccountHandoffStreamingPinBeatsAffinity(t *testing.T) {
	ctx := context.Background()
	selector := NewSessionAffinitySelector(&FillFirstSelector{})
	defer selector.Stop()
	manager := NewManager(nil, selector, nil)
	manager.SetRetryConfig(0, 0, 0)
	const model = "bridge-stream-pin-fixture-model"
	for _, a := range []*Auth{
		{ID: "bridge-pin-old", Provider: "codex", Status: StatusActive, Attributes: map[string]string{"priority": "100"}},
		{ID: "bridge-pin-new", Provider: "codex", Status: StatusActive, Attributes: map[string]string{"priority": "0"}},
		{ID: "bridge-pin-disabled", Provider: "codex", Status: StatusDisabled, Disabled: true},
	} {
		if _, err := manager.Register(ctx, a); err != nil {
			t.Fatal(err)
		}
		registry.GetGlobalRegistry().RegisterClient(a.ID, a.Provider, []*registry.ModelInfo{{ID: model}})
		defer registry.GetGlobalRegistry().UnregisterClient(a.ID)
	}
	var executed []string
	var failID string
	executor := &bridgeHandoffExecutor{}
	executor.identifier = "codex"
	executor.streamFn = func(_ context.Context, a *Auth, _ ex.Request, _ ex.Options) (*ex.StreamResult, error) {
		executed = append(executed, a.ID)
		if a.ID == failID {
			return nil, bridgePinQuotaError{}
		}
		chunks := make(chan ex.StreamChunk, 1)
		chunks <- ex.StreamChunk{Payload: []byte(a.ID)}
		close(chunks)
		return &ex.StreamResult{Chunks: chunks}, nil
	}
	manager.RegisterExecutor(executor)
	request := func(pin string) (string, error) {
		opts := ex.Options{Headers: http.Header{"X-Session-Id": []string{"bridge-same-session"}}}
		if pin != "" {
			opts.Metadata = map[string]any{ex.PinnedAuthMetadataKey: pin}
		}
		stream, err := manager.ExecuteStream(ctx, []string{"codex"}, ex.Request{Model: model}, opts)
		if err != nil {
			return "", err
		}
		var value string
		for chunk := range stream.Chunks {
			if chunk.Err != nil {
				return "", chunk.Err
			}
			value += string(chunk.Payload)
		}
		return value, nil
	}
	if got, err := request(""); err != nil || got != "bridge-pin-old" {
		t.Fatalf("initial affinity: got %q, error %v", got, err)
	}
	if bound, status := manager.LookupSessionAffinity("codex", model, "bridge-same-session"); status != "bound" || bound == nil || bound.ID != "bridge-pin-old" {
		t.Fatalf("old account affinity was not established: %s", status)
	}
	if got, err := request("bridge-pin-new"); err != nil || got != "bridge-pin-new" {
		t.Fatalf("pinned stream against existing affinity: got %q, error %v", got, err)
	}
	if got, err := request("bridge-pin-old"); err != nil || got != "bridge-pin-old" {
		t.Fatalf("pinned old stream: got %q, error %v", got, err)
	}
	for _, pin := range []string{"bridge-pin-missing", "bridge-pin-disabled"} {
		count := len(executed)
		if got, err := request(pin); err == nil {
			t.Fatalf("%s unexpectedly executed %q", pin, got)
		}
		if len(executed) != count {
			t.Fatalf("%s fell back to another account", pin)
		}
	}

	manager.SetRetryConfig(2, 0, 0)
	failID = "bridge-pin-new"
	count := len(executed)
	if got, err := request(failID); err == nil {
		t.Fatalf("rate limited pin unexpectedly succeeded: %q", got)
	}
	if len(executed) == count {
		t.Fatal("rate limited pin was not exercised")
	}
	for _, id := range executed[count:] {
		if id != failID {
			t.Fatal("rate limited pin fell back to a different account")
		}
	}
}
