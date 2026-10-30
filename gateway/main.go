// A stable, authenticated loopback listener for rolling model backends.
// Backend changes affect new requests; admitted streams keep their snapshot.
// No management listener, external dependency, request log or automatic replay.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxBody = 64 << 20

type backendConfig struct {
	Generation string `json:"generation"`
	Port       int    `json:"port"`
	PID        int    `json:"pid"`
	KeyFile    string `json:"key_file"`
}

type backend struct {
	config backendConfig
	proxy  *httputil.ReverseProxy
	active int
}

type gateway struct {
	mu         sync.Mutex
	root       string
	manifest   string
	key        string
	current    string
	backends   map[string]*backend
	checkOwner func(context.Context, int, int) error
}

func privateFile(root, path string, limit int64) ([]byte, error) {
	path, err := filepath.Abs(path)
	if err != nil {
		return nil, errors.New("invalid private file")
	}
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return nil, errors.New("file must be inside private runtime")
	}
	part := root
	for _, name := range strings.Split(rel, string(os.PathSeparator)) {
		part = filepath.Join(part, name)
		info, statErr := os.Lstat(part)
		if statErr != nil || info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("private file missing or symlinked")
		}
		if runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
			return nil, errors.New("private runtime permissions are too broad")
		}
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, errors.New("private file unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > limit {
		return nil, errors.New("invalid private file size/type")
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, errors.New("private file exceeds limit")
	}
	return data, nil
}

func checkPrivateRoot(root string) error {
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("private runtime directory required")
	}
	if runtime.GOOS != "windows" {
		if info.Mode().Perm() != 0700 {
			return errors.New("runtime directory must have mode 0700")
		}
		return nil
	}
	// Paths are data in the child environment, never interpolated into code.
	script := `$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:CLAUDE_CODEX_INGRESS_ROOT; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; if (!$a.AreAccessRulesProtected) {exit 21}; $n=0; foreach ($r in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {if ($r.AccessControlType -eq 'Allow') {$n++; if ($r.IdentityReference.Value -ne $u) {exit 22}}}; if ($n -eq 0) {exit 23}`
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command", script)
	cmd.Env = append(os.Environ(), "CLAUDE_CODEX_INGRESS_ROOT="+root)
	if err := cmd.Run(); err != nil {
		var failure *exec.ExitError
		if errors.As(err, &failure) {
			return fmt.Errorf("runtime NTFS ACL verification failed (check %d)", failure.ExitCode())
		}
		return errors.New("runtime NTFS ACL must grant access only to the current user")
	}
	return nil
}

func listenerOwner(ctx context.Context, port, pid int) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	owners := map[int]bool{}
	if runtime.GOOS == "windows" {
		data, err := exec.CommandContext(ctx, "netstat", "-ano", "-p", "tcp").Output()
		if err != nil {
			return errors.New("cannot verify backend owner")
		}
		for _, line := range strings.Split(string(data), "\n") {
			f := strings.Fields(line)
			if len(f) >= 5 && f[0] == "TCP" && f[1] == fmt.Sprintf("127.0.0.1:%d", port) && f[2] == "0.0.0.0:0" {
				n, _ := strconv.Atoi(f[len(f)-1])
				owners[n] = true
			}
		}
	} else {
		data, err := exec.CommandContext(ctx, "lsof", "-nP", fmt.Sprintf("-tiTCP:%d", port), "-sTCP:LISTEN").Output()
		if err != nil {
			return errors.New("cannot verify backend owner")
		}
		for _, field := range strings.Fields(string(data)) {
			n, _ := strconv.Atoi(field)
			owners[n] = true
		}
	}
	if len(owners) != 1 || !owners[pid] {
		return errors.New("backend owner changed")
	}
	return nil
}

func newGateway(root, manifest, keyFile string) (*gateway, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, errors.New("invalid runtime")
	}
	if err = checkPrivateRoot(root); err != nil {
		return nil, err
	}
	data, err := privateFile(root, keyFile, 4096)
	if err != nil {
		return nil, err
	}
	key := strings.TrimSpace(string(data))
	if len(key) < 32 {
		return nil, errors.New("local key must contain at least 32 characters")
	}
	return &gateway{root: root, manifest: manifest, key: key, backends: map[string]*backend{}, checkOwner: listenerOwner}, nil
}

func (g *gateway) acquire() (*backend, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	data, err := privateFile(g.root, g.manifest, 65536)
	if err != nil {
		return nil, err
	}
	var cfg backendConfig
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&cfg) != nil || decoder.Decode(new(any)) != io.EOF ||
		!regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`).MatchString(cfg.Generation) || cfg.Port < 1 || cfg.Port > 65535 || cfg.PID < 1 {
		return nil, errors.New("invalid backend manifest")
	}
	b := g.backends[cfg.Generation]
	if b != nil && b.config != cfg {
		return nil, errors.New("generation identifiers cannot be reused for changed backends")
	}
	if b == nil {
		key, err := privateFile(g.root, cfg.KeyFile, 4096)
		if err != nil || len(strings.TrimSpace(string(key))) < 32 {
			return nil, errors.New("invalid backend key")
		}
		target := &url.URL{Scheme: "http", Host: fmt.Sprintf("127.0.0.1:%d", cfg.Port)}
		transport := &http.Transport{DisableKeepAlives: true, Proxy: nil,
			DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
				conn, err := (&net.Dialer{Timeout: 3 * time.Second}).DialContext(ctx, network, address)
				if err != nil {
					return nil, err
				}
				if err = g.checkOwner(ctx, cfg.Port, cfg.PID); err != nil {
					conn.Close()
					return nil, err
				}
				return conn, nil
			}}
		backendKey := strings.TrimSpace(string(key))
		proxy := &httputil.ReverseProxy{Transport: transport, FlushInterval: -1,
			ErrorLog: log.New(io.Discard, "", 0),
			Rewrite: func(p *httputil.ProxyRequest) {
				p.SetURL(target)
				p.Out.Host = target.Host
				p.Out.Header.Del("X-Api-Key")
				p.Out.Header.Set("Authorization", "Bearer "+backendKey)
			},
			ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) { gatewayError(w, 502) }}
		b = &backend{config: cfg, proxy: proxy}
		g.backends[cfg.Generation] = b
	}
	g.current = cfg.Generation
	b.active++
	return b, nil
}

func gatewayError(w http.ResponseWriter, code int) {
	w.Header().Set("Content-Type", "application/json")
	if code >= 500 {
		w.Header().Set("Retry-After", "1")
	}
	w.WriteHeader(code)
	_, _ = io.WriteString(w, `{"error":{"type":"gateway_error","message":"Local gateway request could not be completed"}}`)
}

func (g *gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if key == "" {
		key = r.Header.Get("X-Api-Key")
	}
	if subtle.ConstantTimeCompare([]byte(key), []byte(g.key)) != 1 {
		gatewayError(w, 401)
		return
	}
	if r.Method == "GET" && r.URL.Path == "/bridge/status" {
		g.mu.Lock()
		defer g.mu.Unlock()
		active := map[string]int{}
		for id, b := range g.backends {
			active[id] = b.active
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"current": g.current, "active": active})
		return
	}
	if !strings.HasPrefix(r.URL.Path, "/v1/") || (r.Method != "GET" && r.Method != "POST") {
		gatewayError(w, 404)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		gatewayError(w, 413)
		return
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.TransferEncoding = nil
	b, err := g.acquire()
	if err != nil {
		gatewayError(w, 503)
		return
	}
	defer func() { g.mu.Lock(); b.active--; g.mu.Unlock() }()
	b.proxy.ServeHTTP(w, r)
}

func main() {
	root := flag.String("root", "", "Private runtime directory")
	listen := flag.String("listen", "", "127.0.0.1:PORT only")
	manifest := flag.String("backend-file", "", "Private backend manifest")
	key := flag.String("key-file", "", "Private stable frontend key")
	flag.Parse()
	host, port, err := net.SplitHostPort(*listen)
	n, portErr := strconv.Atoi(port)
	if err != nil || portErr != nil || host != "127.0.0.1" || n < 1 || n > 65535 {
		log.Fatal("listen must be 127.0.0.1:PORT")
	}
	g, err := newGateway(*root, *manifest, *key)
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Addr: *listen, Handler: g, ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20, ErrorLog: log.New(io.Discard, "", 0)}
	log.Fatal(server.ListenAndServe())
}
