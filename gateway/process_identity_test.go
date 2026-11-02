package main

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type observedListener struct {
	net.Listener
	accepted chan struct{}
	bytes    *atomic.Int64
}

type observedConn struct {
	net.Conn
	bytes *atomic.Int64
}

func (c observedConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	c.bytes.Add(int64(n))
	return n, err
}

func (l observedListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	l.accepted <- struct{}{}
	return observedConn{Conn: c, bytes: l.bytes}, nil
}

func TestOriginIdentityCheckedBeforeAnyBackendBytes(t *testing.T) {
	if !identityAvailable() {
		t.Skip("native identity is unavailable in this build")
	}
	g, frontKey, write := fixture(t)
	g.requireIdentity = true
	var received, requests atomic.Int64
	accepted := make(chan struct{}, 16)
	backend := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		_, _ = io.WriteString(w, "fixture")
	}))
	backend.Listener = observedListener{Listener: backend.Listener, accepted: accepted, bytes: &received}
	backend.Start()
	defer backend.Close()
	write("origin-fixture", backend, strings.Repeat("a", 32))
	var cfg backendConfig
	data, err := os.ReadFile(g.manifest)
	if err != nil || json.Unmarshal(data, &cfg) != nil {
		t.Fatal("fixture manifest unavailable")
	}
	actual, err := identity(cfg.PID)
	if err != nil {
		t.Fatal("fixture identity unavailable")
	}
	ownerPath := filepath.Join(g.root, "owner-"+cfg.Generation+".json")
	owner := originIdentity{Generation: cfg.Generation, PID: cfg.PID, Port: cfg.Port, Identity: actual}
	front := httptest.NewServer(g)
	defer front.Close()
	writeOwner := func(value any) {
		t.Helper()
		encoded, err := json.Marshal(value)
		if err != nil || os.WriteFile(ownerPath, encoded, 0600) != nil {
			t.Fatal("fixture owner write failed")
		}
	}
	assertDenied := func() {
		t.Helper()
		res := call(t, front.URL, frontKey, "/v1/messages")
		_, _ = io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if res.StatusCode != 502 {
			t.Fatalf("identity failure returned %d", res.StatusCode)
		}
		select {
		case <-accepted:
		case <-time.After(5 * time.Second):
			t.Fatal("fixture connection was not accepted")
		}
		if received.Load() != 0 || requests.Load() != 0 {
			t.Fatal("backend received bytes before identity verification")
		}
	}
	assertDenied() // Missing sidecar.
	writeOwner(originIdentity{Generation: cfg.Generation, PID: cfg.PID, Port: cfg.Port, Identity: strings.Repeat("0", 64)})
	assertDenied() // Same PID and port, different synthetic process identity.
	writeOwner(originIdentity{Generation: cfg.Generation, PID: cfg.PID, Port: cfg.Port + 1, Identity: actual})
	assertDenied()
	writeOwner(map[string]any{"generation": cfg.Generation, "pid": cfg.PID, "port": cfg.Port,
		"identity": actual, "unrecognized": true})
	assertDenied()
	writeOwner(owner)
	res := call(t, front.URL, frontKey, "/v1/messages")
	_, _ = io.Copy(io.Discard, res.Body)
	res.Body.Close()
	if res.StatusCode != 200 || requests.Load() != 1 {
		t.Fatal("verified fixture origin did not receive request")
	}
	<-accepted
	received.Store(0)
	requests.Store(0)
	writeOwner(originIdentity{Generation: cfg.Generation, PID: cfg.PID, Port: cfg.Port, Identity: strings.Repeat("0", 64)})
	assertDenied() // A cached backend must recheck the sidecar on every dial.
}
