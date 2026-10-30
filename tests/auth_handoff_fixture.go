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
