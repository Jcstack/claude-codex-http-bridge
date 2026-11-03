// Isolated test copied into the pinned upstream cliproxy package. It uses
// synthetic auth records with no credentials and refuses HTTP requests.
package cliproxy

import (
	"context"
	"errors"
	"net/http"
	"reflect"
	"sort"
	"sync/atomic"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/config"
)

type bridgeCatalogBlockingTransport struct{ calls atomic.Int32 }

func (transport *bridgeCatalogBlockingTransport) RoundTrip(*http.Request) (*http.Response, error) {
	transport.calls.Add(1)
	return nil, errors.New("synthetic test blocks network")
}

func bridgeCatalogIDs(models []*registry.ModelInfo) []string {
	ids := make([]string, 0, len(models))
	seen := make(map[string]bool)
	for _, model := range models {
		if model != nil && model.ID != "" && !seen[model.ID] {
			ids = append(ids, model.ID)
			seen[model.ID] = true
		}
	}
	sort.Strings(ids)
	return ids
}

func TestBridgeCodexCatalogProvenance(t *testing.T) {
	previousTransport := http.DefaultTransport
	blocked := &bridgeCatalogBlockingTransport{}
	http.DefaultTransport = blocked
	defer func() { http.DefaultTransport = previousTransport }()

	manager := coreauth.NewManager(nil, nil, nil)
	service := &Service{cfg: &config.Config{}, coreManager: manager}
	cases := []struct {
		id, plan string
		want     []*registry.ModelInfo
	}{
		{"bridge-fixture-pro", "pro", registry.GetCodexProModels()},
		{"bridge-fixture-plus", "plus", registry.GetCodexPlusModels()},
		{"bridge-fixture-unknown", "unrecognized", registry.GetCodexProModels()},
	}
	for _, tc := range cases {
		registry.GetGlobalRegistry().UnregisterClient(tc.id)
		t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(tc.id) })
		auth := &coreauth.Auth{ID: tc.id, Provider: "codex", Status: coreauth.StatusActive,
			Attributes: map[string]string{"plan_type": tc.plan, "auth_kind": "oauth"}}
		registered, err := manager.Register(context.Background(), auth)
		if err != nil {
			t.Fatal("synthetic registration failed")
		}
		if !service.registerModelsForAuth(context.Background(), registered) {
			t.Fatal("synthetic catalog publication was fenced")
		}
		got := bridgeCatalogIDs(registry.GetGlobalRegistry().GetModelsForClient(tc.id))
		want := bridgeCatalogIDs(tc.want)
		if len(want) == 0 || !reflect.DeepEqual(got, want) {
			t.Fatal("synthetic registered IDs differ from the embedded plan catalog")
		}
	}
	if blocked.calls.Load() != 0 {
		t.Fatal("synthetic Codex model registration unexpectedly attempted HTTP")
	}
}
