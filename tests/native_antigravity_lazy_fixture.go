// Isolated candidate test copied into the pinned upstream misc package.
// The only transport is an in-memory synthetic response; no credentials or
// external network are involved. Run this test alone in its own process.
package misc

import (
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

type bridgeRoundTripFunc func(*http.Request) (*http.Response, error)

func (f bridgeRoundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestBridgeLazyAntigravityVersion(t *testing.T) {
	requests := make(chan *http.Request, 64)
	previousTransport := http.DefaultTransport
	http.DefaultTransport = bridgeRoundTripFunc(func(request *http.Request) (*http.Response, error) {
		requests <- request
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader("version: 2.99.0\n")), Request: request}, nil
	})
	defer func() { http.DefaultTransport = previousTransport }()

	var workers sync.WaitGroup
	for i := 0; i < 32; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			_ = AntigravityLatestVersion()
		}()
	}
	workers.Wait()
	var request *http.Request
	select {
	case request = <-requests:
	case <-time.After(5 * time.Second):
		t.Fatal("version use did not start the synthetic refresh")
	}
	if request.Method != http.MethodGet || request.Body != nil || request.URL.User != nil ||
		request.URL.String() != antigravityHubLatestManifestURL ||
		request.Header.Get("Authorization") != "" || request.Header.Get("Cookie") != "" ||
		request.Header.Get("User-Agent") != "electron-builder" || request.Header.Get("Cache-Control") != "no-cache" {
		t.Fatal("unexpected synthetic request construction")
	}
	deadline := time.Now().Add(5 * time.Second)
	for AntigravityLatestVersion() != "2.99.0" {
		if time.Now().After(deadline) {
			t.Fatal("refreshed synthetic version was not published")
		}
		time.Sleep(time.Millisecond)
	}
	select {
	case <-requests:
		t.Fatal("concurrent version reads started multiple refreshes")
	case <-time.After(200 * time.Millisecond):
	}
}
