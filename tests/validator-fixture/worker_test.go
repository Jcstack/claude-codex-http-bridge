package probe

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// This is an isolated-process fixture, not a production worker or OS sandbox.
// It deliberately has no memory quota and does not guard real tool streams.
type syntheticWorkerRequest struct {
	Schema string `json:"schema"`
	Input  string `json:"input"`
}

func TestSyntheticWorkerChild(t *testing.T) {
	mode := os.Getenv("BRIDGE_SYNTHETIC_VALIDATOR_CHILD")
	if mode == "" {
		return
	}
	defer os.Exit(0)
	fmt.Fprintln(os.Stdout, "READY")
	if mode == "hang" {
		time.Sleep(30 * time.Second)
		return
	}
	if mode == "flood" {
		fmt.Fprintln(os.Stdout, strings.Repeat("SYNTHETIC_PRIVATE_SENTINEL", 1000))
		return
	}
	if mode == "envcheck" {
		if os.Getenv("BRIDGE_SYNTHETIC_SECRET") == "" {
			fmt.Fprintln(os.Stdout, "valid")
		} else {
			fmt.Fprintln(os.Stdout, "refused")
		}
		return
	}
	if mode != "validate" {
		fmt.Fprintln(os.Stdout, "refused")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 2*maxBytes+1025))
	var request syntheticWorkerRequest
	if err != nil || len(raw) > 2*maxBytes+1024 || json.Unmarshal(raw, &request) != nil {
		fmt.Fprintln(os.Stdout, "refused")
		return
	}
	schema, err := compile(request.Schema, &denyLoader{})
	if err != nil {
		fmt.Fprintln(os.Stdout, "refused")
		return
	}
	value, err := boundedJSON(request.Input)
	if err != nil {
		fmt.Fprintln(os.Stdout, "refused")
		return
	}
	if schema.Validate(value) != nil {
		fmt.Fprintln(os.Stdout, "invalid")
		return
	}
	fmt.Fprintln(os.Stdout, "valid")
}

type syntheticWorkerPool struct {
	active chan struct{}
	home   string
}

func (pool *syntheticWorkerPool) run(ctx context.Context, mode string, request syntheticWorkerRequest,
	ready chan<- struct{}) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	select {
	case pool.active <- struct{}{}:
		defer func() { <-pool.active }()
	case <-ctx.Done():
		return "", ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	encoded, err := json.Marshal(request)
	if err != nil || len(encoded) > 2*maxBytes+1024 {
		return "refused", nil
	}
	executable, err := os.Executable()
	if err != nil {
		return "", errors.New("worker unavailable")
	}
	command := exec.CommandContext(ctx, executable, "-test.run=^TestSyntheticWorkerChild$")
	command.Env = []string{"HOME=" + pool.home, "USERPROFILE=" + pool.home,
		"BRIDGE_SYNTHETIC_VALIDATOR_CHILD=" + mode}
	for _, key := range []string{"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT"} {
		if value, ok := os.LookupEnv(key); ok {
			command.Env = append(command.Env, key+"="+value)
		}
	}
	command.Stdin = bytes.NewReader(encoded)
	command.Stderr = io.Discard
	stdout, err := command.StdoutPipe()
	if err != nil {
		return "", errors.New("worker unavailable")
	}
	if err := command.Start(); err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", errors.New("worker unavailable")
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64), 64)
	result := ""
	protocolOK := scanner.Scan() && scanner.Text() == "READY"
	if protocolOK && ready != nil {
		close(ready)
	}
	if protocolOK && scanner.Scan() {
		result = scanner.Text()
		protocolOK = !scanner.Scan() && scanner.Err() == nil
	} else {
		protocolOK = false
	}
	if !protocolOK && command.Process != nil {
		_ = command.Process.Kill()
	}
	waitErr := command.Wait()
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if !protocolOK || waitErr != nil || (result != "valid" && result != "invalid" && result != "refused") {
		return "", errors.New("worker failed")
	}
	return result, nil
}

func TestSyntheticWorkerIsolation(t *testing.T) {
	pool := syntheticWorkerPool{active: make(chan struct{}, 1), home: t.TempDir()}
	t.Setenv("BRIDGE_SYNTHETIC_SECRET", "SYNTHETIC_PRIVATE_SENTINEL")
	secretCtx, stopSecret := context.WithTimeout(context.Background(), 8*time.Second)
	secretResult, secretErr := pool.run(secretCtx, "envcheck", syntheticWorkerRequest{}, nil)
	stopSecret()
	if secretErr != nil || secretResult != "valid" {
		t.Fatal("synthetic worker inherited the unrelated fixture variable")
	}
	const schema = `{"type":"object","properties":{"value":{"type":"string"}},"required":["value"]}`
	for _, row := range []struct{ input, want string }{
		{`{"value":"synthetic"}`, "valid"},
		{`{"value":123}`, "invalid"},
		{`{"value":1,"value":2}`, "refused"},
		{`{"value":"\ud800"}`, "refused"},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		got, err := pool.run(ctx, "validate", syntheticWorkerRequest{Schema: schema, Input: row.input}, nil)
		cancel()
		if err != nil || got != row.want {
			t.Fatal("synthetic worker validation control failed")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	got, err := pool.run(ctx, "validate", syntheticWorkerRequest{Schema: `{"const":"SYNTHETIC_PRIVATE_SENTINEL"}`,
		Input: `"different"`}, nil)
	cancel()
	if err != nil || got != "invalid" || strings.Contains(got, "SYNTHETIC_PRIVATE_SENTINEL") {
		t.Fatal("synthetic worker error output was not sanitized")
	}
	ctx, cancel = context.WithTimeout(context.Background(), 8*time.Second)
	got, err = pool.run(ctx, "validate", syntheticWorkerRequest{Schema: `{"$ref":"https://external.invalid/schema"}`,
		Input: `"synthetic"`}, nil)
	cancel()
	if err != nil || got != "refused" {
		t.Fatal("synthetic worker external reference was not refused")
	}
	ctx, cancel = context.WithTimeout(context.Background(), 8*time.Second)
	got, err = pool.run(ctx, "flood", syntheticWorkerRequest{}, nil)
	cancel()
	if err == nil || got != "" || strings.Contains(err.Error(), "SYNTHETIC_PRIVATE_SENTINEL") {
		t.Fatal("synthetic worker accepted oversized or private output")
	}
}

func TestSyntheticWorkerCancellationAndLimit(t *testing.T) {
	pool := syntheticWorkerPool{active: make(chan struct{}, 1), home: t.TempDir()}
	canceled, stopCanceled := context.WithCancel(context.Background())
	stopCanceled()
	if _, err := pool.run(canceled, "validate", syntheticWorkerRequest{}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("pre-canceled synthetic work was not refused")
	}
	ctx, cancel := context.WithCancel(context.Background())
	ready := make(chan struct{})
	finished := make(chan error, 1)
	go func() {
		_, err := pool.run(ctx, "hang", syntheticWorkerRequest{}, ready)
		finished <- err
	}()
	select {
	case <-ready:
	case <-time.After(8 * time.Second):
		cancel()
		t.Fatal("synthetic worker did not become ready")
	}
	waitCtx, stopWait := context.WithTimeout(context.Background(), 150*time.Millisecond)
	_, err := pool.run(waitCtx, "validate", syntheticWorkerRequest{}, nil)
	stopWait()
	if !errors.Is(err, context.DeadlineExceeded) {
		cancel()
		t.Fatal("worker concurrency limit was not enforced")
	}
	cancel()
	select {
	case err := <-finished:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("synthetic worker cancellation was not reported")
		}
	case <-time.After(8 * time.Second):
		t.Fatal("synthetic worker did not terminate after cancellation")
	}
	probeCtx, stopProbe := context.WithTimeout(context.Background(), 8*time.Second)
	defer stopProbe()
	got, err := pool.run(probeCtx, "validate", syntheticWorkerRequest{Schema: `{"type":"string"}`, Input: `"ok"`}, nil)
	if err != nil || got != "valid" {
		t.Fatal("worker slot was not reusable after cancellation")
	}
}
