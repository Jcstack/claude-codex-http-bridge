// Copy into the pinned translator package as *_test.go for a synthetic benchmark.
package claude

import (
	"encoding/json"
	"fmt"
	"testing"
)

func BenchmarkToolContractFixture(b *testing.B) {
	for _, explicit := range []bool{false, true} {
		name := "ordinary"
		if explicit {
			name = "explicit_direct"
		}
		b.Run(name, func(b *testing.B) {
			messages := make([]any, 1000)
			for i := range messages {
				messages[i] = map[string]any{"role": "user", "content": "synthetic fixture text repeated for a bounded history"}
			}
			tools := make([]any, 100)
			for i := range tools {
				tool := map[string]any{"name": fmt.Sprintf("fixture_%03d", i), "input_schema": map[string]any{
					"type": "object", "properties": map[string]any{"value": map[string]any{"type": "string"}}}}
				if explicit {
					tool["allowed_callers"] = []string{"direct"}
					tool["strict"] = false
				}
				tools[i] = tool
			}
			raw, err := json.Marshal(map[string]any{"messages": messages, "tools": tools})
			if err != nil {
				b.Fatal(err)
			}
			b.ReportAllocs()
			b.SetBytes(int64(len(raw)))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := ConvertClaudeRequestToCodex("fixture", raw, false); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}
