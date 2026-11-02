package handlers

import (
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
)

type bridgeWrappedEffortError struct{}

func (bridgeWrappedEffortError) Error() string { return "generic wrapped failure" }
func (bridgeWrappedEffortError) ResponseBody() []byte {
	return []byte(`{"error":{"message":"model does not support the effort parameter PRIVATE_FIXTURE"}}`)
}

func TestBridgeMarkClaudeRequestedEffort(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cases := []struct {
		body string
		want bool
	}{
		{`{"output_config":{"effort":"max"}}`, true},
		{`{"messages":[{"role":"system","output_config":{"effort":"high"}}]}`, true},
		{`{"output_config":{"effort":null}}`, false},
		{`{"messages":[{"role":"user","output_config":{"effort":"max"}}]}`, false},
		{`{"messages":[{"role":"user","content":"output_config.effort"}]}`, false},
		{`{"output_config":{"effort":123}}`, false},
	}
	for _, tc := range cases {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		MarkClaudeRequestedEffort(c, []byte(tc.body))
		if got := c.GetBool(bridgeClaudeEffortKey); got != tc.want {
			t.Fatalf("explicit effort marker: got %v, want %v", got, tc.want)
		}
	}
}

func TestBridgeProtectClaudeEffortError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cases := []struct {
		name      string
		marked    bool
		committed bool
		msg       *interfaces.ErrorMessage
		want      bool
	}{
		{"effort field", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("output_config.effort: Extra inputs are not permitted PRIVATE_FIXTURE")}, true},
		{"mid conversation extra input", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("messages.3.output_config: Extra inputs are not permitted PRIVATE_FIXTURE")}, true},
		{"mid conversation model", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("output_config.effort requires a model that supports per-turn effort; this model does not PRIVATE_FIXTURE")}, true},
		{"model rejection", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("model does not support the effort parameter PRIVATE_FIXTURE")}, true},
		{"beta rejection", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("Unexpected value(s) effort-2025-11-24 in anthropic-beta PRIVATE_FIXTURE")}, true},
		{"json error", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New(`{"error":{"message":"output_config.effort: Extra inputs are not permitted PRIVATE_FIXTURE"}}`)}, true},
		{"wrapped body", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: fmt.Errorf("wrapped: %w", bridgeWrappedEffortError{})}, true},
		{"direct error", true, false, &interfaces.ErrorMessage{StatusCode: 400, DirectResponse: true, Body: []byte(`{"error":{"message":"model does not support the effort parameter PRIVATE_FIXTURE"}}`)}, true},
		{"unmarked", false, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("output_config.effort: Extra inputs are not permitted")}, false},
		{"other status", true, false, &interfaces.ErrorMessage{StatusCode: 429, Error: errors.New("output_config.effort: Extra inputs are not permitted")}, false},
		{"other field", true, false, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("context_management: Extra inputs are not permitted")}, false},
		{"committed", true, true, &interfaces.ErrorMessage{StatusCode: 400, Error: errors.New("output_config.effort: Extra inputs are not permitted")}, false},
		{"nil", true, false, nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			if tc.marked {
				c.Set(bridgeClaudeEffortKey, true)
			}
			if tc.committed {
				c.Status(http.StatusOK)
				c.Writer.WriteHeaderNow()
			}
			if got := ProtectClaudeEffortError(c, tc.msg); got != tc.want {
				t.Fatalf("protected=%v, want %v", got, tc.want)
			}
			if tc.want && (w.Code != 400 || strings.Contains(w.Body.String(), "PRIVATE_FIXTURE") ||
				!strings.Contains(w.Body.String(), "not downgraded") || w.Header().Get("x-should-retry") != "false") {
				t.Fatal("protected response must be explicit, sanitized and non-retryable")
			}
		})
	}
}
