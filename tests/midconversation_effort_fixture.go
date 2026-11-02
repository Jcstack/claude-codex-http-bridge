package handlers

import (
	"fmt"
	"testing"

	"github.com/tidwall/gjson"
)

func TestBridgeMidConversationEffort(t *testing.T) {
	const user = `{"role":"user","content":"request"}`
	const assistant = `{"role":"assistant","content":"reply"}`
	const toolOnly = `{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"ok"}]}`
	const mixed = `{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"ok"},{"type":"text","text":"new request"}]}`
	const low = `{"role":"system","content":[],"output_config":{"effort":"low"}}`
	const max = `{"role":"system","content":[],"output_config":{"effort":"max"}}`
	cases := []struct {
		name, messages, want string
	}{
		{"top level control", user, "high"},
		{"before new input", user + "," + assistant + "," + low + "," + user, "low"},
		{"immediately after new input", user + "," + low, "low"},
		{"consecutive system directives", user + "," + low + "," + max, "max"},
		{"pending after assistant", user + "," + assistant + "," + low, "high"},
		{"pending across tool results", user + "," + assistant + "," + low + "," + toolOnly, "high"},
		{"pending applies on next new input", user + "," + assistant + "," + low + "," + toolOnly + "," + user, "low"},
		{"mixed tool results and text are new input", user + "," + assistant + "," + low + "," + mixed, "low"},
		{"applied level retained while next directive pending", user + "," + low + "," + assistant + "," + max + "," + toolOnly, "low"},
		{"latest pending directive wins", user + "," + assistant + "," + low + "," + max + "," + user, "max"},
		{"new user after compaction", `{"role":"system","content":[],"output_config":{"effort":"low"}},` + user, "low"},
		{"non system effort ignored", `{"role":"user","content":"x","output_config":{"effort":"low"}}`, "high"},
		{"future effort remains dynamic", user + `,{"role":"system","content":[],"output_config":{"effort":"future"}}`, "future"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := []byte(fmt.Sprintf(`{"output_config":{"effort":"high","format":{"type":"json_schema"}},"messages":[%s]}`, tc.messages))
			updated, err := ResolveClaudeMidConversationEffort(body)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got := gjson.GetBytes(updated, "output_config.effort").String(); got != tc.want {
				t.Fatalf("effective effort = %q, want %q", got, tc.want)
			}
			if got, want := gjson.GetBytes(updated, "messages").Raw, gjson.GetBytes(body, "messages").Raw; got != want {
				t.Fatal("conversation history was modified")
			}
			if gjson.GetBytes(updated, "output_config.format.type").String() != "json_schema" {
				t.Fatal("unrelated output format was modified")
			}
		})
	}
}

func TestBridgeMidConversationEffortInvalid(t *testing.T) {
	for _, body := range []string{
		`{"messages":[{"role":"user","content":"x"},{"role":"system","output_config":{"effort":42}}]}`,
		`{"messages":[{"role":"user","content":"x"},{"role":"system","output_config":{"effort":""}}]}`,
		`{"messages":[{"role":"user","content":"x"},{"role":"system","clear_at":"next_user_message","output_config":{"effort":"low"}}]}`,
		`{"output_config":"bad","messages":[{"role":"user","content":"x"},{"role":"system","output_config":{"effort":"low"}}]}`,
	} {
		if _, err := ResolveClaudeMidConversationEffort([]byte(body)); err == nil {
			t.Fatal("invalid reasoning directive was silently accepted")
		}
	}
}
