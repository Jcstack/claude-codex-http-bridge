package handlers

import "testing"

func TestBridgeScopedSystemValidation(t *testing.T) {
	tests := []struct {
		name, body string
		valid      bool
	}{
		{"ordinary", `{"messages":[{"role":"user","content":"hello"}]}`, true},
		{"never", `{"messages":[{"role":"system","clear_at":"never","content":"hello"}]}`, true},
		{"scoped string", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":"hello"}]}`, true},
		{"scoped blocks", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":[{"type":"text","text":"hello"}]}]}`, true},
		{"scoped system first", `{"messages":[{"role":"system","clear_at":"next_user_message","content":"hello"}]}`, false},
		{"followed directly by user", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":"hello"},{"role":"user","content":"y"}]}`, false},
		{"wrong role", `{"messages":[{"role":"user","clear_at":"next_user_message","content":"hello"}]}`, false},
		{"unknown scope", `{"messages":[{"role":"system","clear_at":"unexpected","content":"hello"}]}`, false},
		{"non-text", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":[{"type":"tool_removal"}]}]}`, false},
		{"effort", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":"hello","output_config":{"effort":"low"}}]}`, false},
		{"cache marker", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":[{"type":"text","text":"hello","cache_control":{"type":"ephemeral"}}]}]}`, false},
		{"empty array", `{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","content":[]}]}`, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateClaudeTurnScopedSystem([]byte(tc.body))
			if (err == nil) != tc.valid {
				t.Fatalf("valid = %v, want %v", err == nil, tc.valid)
			}
		})
	}
}
