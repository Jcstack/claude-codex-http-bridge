package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T) (*gateway, string, func(string, *httptest.Server, string)) {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		script := `$ErrorActionPreference='Stop'; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User; $a=New-Object Security.AccessControl.DirectorySecurity; $a.SetOwner($u); $a.SetAccessRuleProtection($true,$false); $r=New-Object Security.AccessControl.FileSystemAccessRule($u,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); $a.AddAccessRule($r); (Get-Item -LiteralPath $env:CLAUDE_CODEX_INGRESS_ROOT).SetAccessControl($a)`
		cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", script)
		cmd.Env = append(os.Environ(), "CLAUDE_CODEX_INGRESS_ROOT="+root)
		if cmd.Run() != nil {
			t.Fatal("could not lock fixture NTFS directory")
		}
	}
	key := strings.Repeat("f", 32)
	keyPath := filepath.Join(root, "front-key")
	if err := os.WriteFile(keyPath, []byte(key), 0600); err != nil {
		t.Fatal(err)
	}
	manifest := filepath.Join(root, "backend.json")
	g, err := newGateway(root, manifest, keyPath)
	if err != nil {
		t.Fatal(err)
	}
	g.checkOwner = func(context.Context, int, int) error { return nil }
	write := func(id string, server *httptest.Server, backendKey string) {
		t.Helper()
		u, _ := url.Parse(server.URL)
		port, _ := strconv.Atoi(u.Port())
		path := filepath.Join(root, id+"-key")
		if err := os.WriteFile(path, []byte(backendKey), 0600); err != nil {
			t.Fatal(err)
		}
		data, _ := json.Marshal(backendConfig{Generation: id, Port: port, PID: os.Getpid(), KeyFile: path})
		staged := filepath.Join(root, "pending.json")
		if err := os.WriteFile(staged, data, 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(staged, manifest); err != nil {
			t.Fatal(err)
		}
	}
	return g, key, write
}

func call(t *testing.T, server, key, path string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest("POST", server+path, strings.NewReader(`{"model":"fixture"}`))
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("X-Claude-Code-Session-Id", "fixture-stable-session")
	res, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func TestRollingBackendPreservesAdmittedStreamAndIdentity(t *testing.T) {
	g, key, write := fixture(t)
	release := make(chan struct{})
	aHeaders := make(chan http.Header, 1)
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		aHeaders <- r.Header.Clone()
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: A-first\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		_, _ = io.WriteString(w, "data: A-last\n\n")
	}))
	defer a.Close()
	bHeaders := make(chan http.Header, 1)
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bHeaders <- r.Header.Clone()
		_, _ = io.WriteString(w, "B-only")
	}))
	defer b.Close()
	write("first", a, strings.Repeat("a", 32))
	front := httptest.NewServer(g)
	defer front.Close()
	first := call(t, front.URL, key, "/v1/messages")
	defer first.Body.Close()
	reader := bufio.NewReader(first.Body)
	line, err := reader.ReadString('\n')
	if err != nil || line != "data: A-first\n" {
		t.Fatal("first stream was buffered or changed")
	}
	write("second", b, strings.Repeat("b", 32))
	second := call(t, front.URL, key, "/v1/messages")
	data, _ := io.ReadAll(second.Body)
	second.Body.Close()
	if string(data) != "B-only" {
		t.Fatal("new request did not reach replacement")
	}
	g.mu.Lock()
	active := g.backends["first"].active
	g.mu.Unlock()
	if active != 1 {
		t.Fatal("old stream was not retained during switch")
	}
	close(release)
	data, err = io.ReadAll(reader)
	if err != nil || !strings.Contains(string(data), "A-last") || strings.Contains(string(data), "B-only") {
		t.Fatal("admitted stream did not finish on old backend")
	}
	for expected, headers := range map[string]http.Header{strings.Repeat("a", 32): <-aHeaders, strings.Repeat("b", 32): <-bHeaders} {
		if headers.Get("Authorization") != "Bearer "+expected || headers.Get("X-Claude-Code-Session-Id") != "fixture-stable-session" {
			t.Fatal("credential replacement or session preservation failed")
		}
	}
}

func TestClientCancellationReachesBackend(t *testing.T) {
	g, key, write := fixture(t)
	cancelled := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(cancelled)
	}))
	defer upstream.Close()
	write("first", upstream, strings.Repeat("a", 32))
	front := httptest.NewServer(g)
	defer front.Close()
	res := call(t, front.URL, key, "/v1/messages")
	_, _ = bufio.NewReader(res.Body).ReadString('\n')
	res.Body.Close()
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("disconnect did not cancel upstream")
	}
}

func TestUnauthorizedAndWrongOwnerDoNotSendCredentials(t *testing.T) {
	g, key, write := fixture(t)
	called := make(chan struct{}, 2)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called <- struct{}{} }))
	defer upstream.Close()
	write("first", upstream, strings.Repeat("a", 32))
	front := httptest.NewServer(g)
	defer front.Close()
	res := call(t, front.URL, "wrong", "/v1/messages")
	res.Body.Close()
	if res.StatusCode != 401 {
		t.Fatal("unauthenticated caller accepted")
	}
	res = call(t, front.URL, key, "/v0/management/config")
	res.Body.Close()
	if res.StatusCode != 404 {
		t.Fatal("management route exposed")
	}
	g.checkOwner = func(context.Context, int, int) error { return errors.New("wrong owner") }
	res = call(t, front.URL, key, "/v1/messages")
	res.Body.Close()
	if res.StatusCode != 502 {
		t.Fatal("wrong backend owner accepted")
	}
	select {
	case <-called:
		t.Fatal("backend received an unauthorized request")
	default:
	}
}

func TestUnsafeManifestFailsClosed(t *testing.T) {
	g, key, write := fixture(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "safe") }))
	defer upstream.Close()
	write("first", upstream, strings.Repeat("a", 32))
	front := httptest.NewServer(g)
	defer front.Close()
	res := call(t, front.URL, key, "/v1/messages")
	res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatal("valid backend rejected")
	}
	if err := os.WriteFile(g.manifest, []byte(`{"generation":"bad","port":80,"pid":1,"key_file":"/outside-private-runtime"}`), 0600); err != nil {
		t.Fatal(err)
	}
	res = call(t, front.URL, key, "/v1/messages")
	data, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 503 || strings.Contains(string(data), "outside-private") {
		t.Fatal("unsafe manifest accepted or path leaked")
	}
	write("first", upstream, strings.Repeat("a", 32))
	res = call(t, front.URL, key, "/v1/messages")
	res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatal("safe rollback failed")
	}
}

func TestOriginRequiresSeparateKeyAndCannotReachManagement(t *testing.T) {
	front, frontKey, _ := fixture(t)
	origin, originKey, write := fixture(t)
	// fixture() uses the same synthetic key by default; distinct realms matter.
	originKey = strings.Repeat("o", 32)
	origin.key = originKey
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/responses" || r.Header.Get("Authorization") != "Bearer "+strings.Repeat("a", 32) {
			t.Error("origin path or key was not rewritten")
		}
		_, _ = io.WriteString(w, "origin-fixture")
	}))
	defer upstream.Close()
	write("origin", upstream, strings.Repeat("a", 32))
	server := httptest.NewServer(withOrigin(front, origin))
	defer server.Close()
	for _, key := range []string{frontKey, "invalid"} {
		res := call(t, server.URL, key, "/bridge/origin/v1/responses")
		res.Body.Close()
		if res.StatusCode != 401 {
			t.Fatal("frontend key reached origin")
		}
	}
	res := call(t, server.URL, originKey, "/bridge/origin/v1/responses")
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 200 || string(body) != "origin-fixture" {
		t.Fatal("origin forwarding failed")
	}
	for _, path := range []string{"/bridge/origin/bridge/status", "/bridge/origin/v0/management/api-call"} {
		res := call(t, server.URL, originKey, path)
		res.Body.Close()
		if res.StatusCode != 404 {
			t.Fatal("origin exposed management")
		}
	}
}

func TestLocalIdentitySurvivesRenewalAndHalfWrittenKeyPair(t *testing.T) {
	g, _, _ := fixture(t)
	if err := initializeTLS(g.root); err != nil {
		t.Fatal(err)
	}
	ca, err := os.ReadFile(filepath.Join(g.root, "ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	provider, err := runtimeCertificateProvider(g.root)
	if err != nil {
		t.Fatal(err)
	}
	before, err := provider(nil)
	if err != nil {
		t.Fatal(err)
	}
	keyPath := filepath.Join(g.root, "server-key.pem")
	key, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyPath, []byte("half-written-fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	during, err := provider(nil)
	if err != nil || string(during.Certificate[0]) != string(before.Certificate[0]) {
		t.Fatal("valid identity was lost during rotation")
	}
	if err := os.WriteFile(keyPath, key, 0600); err != nil {
		t.Fatal(err)
	}
	if err := initializeTLS(g.root); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(filepath.Join(g.root, "ca.pem"))
	if err != nil || string(after) != string(ca) {
		t.Fatal("routine renewal replaced the trusted CA")
	}
}
