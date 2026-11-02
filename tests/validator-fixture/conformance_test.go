package probe

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Public, pinned JSON Schema Test Suite data only. This is a selected subset,
// not a conformance certificate, and no remote schemas are fetched.
func TestSelectedOfficialDraft2020Cases(t *testing.T) {
	root := os.Getenv("BRIDGE_JSONSCHEMA_SUITE")
	if root == "" {
		if os.Getenv("BRIDGE_REQUIRE_CONFORMANCE") == "1" {
			t.Fatal("the pinned public test suite is required")
		}
		t.Skip("set BRIDGE_JSONSCHEMA_SUITE to the pinned public test suite")
	}
	files := []string{"type", "const", "required", "properties", "additionalProperties", "pattern",
		"patternProperties", "enum", "minimum", "maximum", "multipleOf", "minLength", "maxLength",
		"anyOf", "allOf", "oneOf", "not", "if-then-else", "ref", "items", "prefixItems", "contains",
		"uniqueItems", "format", "boolean_schema"}
	type testCase struct {
		Data  json.RawMessage `json:"data"`
		Valid *bool           `json:"valid"`
	}
	type group struct {
		Schema json.RawMessage `json:"schema"`
		Tests  []testCase      `json:"tests"`
	}
	total, valid, invalid, compileRejected, inputRejected := 0, 0, 0, 0, 0
	falseAccepted, falseRejected := 0, 0
	for _, name := range files {
		path := filepath.Join(root, "tests", "draft2020-12", name+".json")
		raw, err := os.ReadFile(path)
		if err != nil || len(raw) > 4*1024*1024 {
			t.Fatalf("public suite file unavailable or oversized: %s", name)
		}
		var groups []group
		if err := json.Unmarshal(raw, &groups); err != nil || len(groups) == 0 {
			t.Fatalf("invalid public suite file: %s", name)
		}
		fileChecked, fileCompileRejected, fileInputRejected, fileFalseAccepted, fileFalseRejected := 0, 0, 0, 0, 0
		mismatches := []string{}
		rejectedInputs := []string{}
		for groupIndex, group := range groups {
			if len(group.Schema) == 0 || len(group.Tests) == 0 {
				t.Fatalf("invalid public suite group: %s", name)
			}
			schema, err := compile(string(group.Schema), &denyLoader{})
			if err != nil {
				fileCompileRejected += len(group.Tests)
				continue
			}
			for caseIndex, item := range group.Tests {
				if item.Valid == nil || len(item.Data) == 0 {
					t.Fatalf("invalid public suite case: %s", name)
				}
				value, err := boundedJSON(string(item.Data))
				if err != nil {
					fileInputRejected++
					if len(rejectedInputs) < 20 {
						rejectedInputs = append(rejectedInputs, fmt.Sprintf("%d.%d", groupIndex, caseIndex))
					}
					continue
				}
				got := schema.Validate(value) == nil
				fileChecked++
				if *item.Valid {
					valid++
				} else {
					invalid++
				}
				if got != *item.Valid {
					if got {
						fileFalseAccepted++
					} else {
						fileFalseRejected++
					}
					if len(mismatches) < 20 {
						mismatches = append(mismatches, fmt.Sprintf("%d.%d", groupIndex, caseIndex))
					}
				}
			}
		}
		total += fileChecked
		compileRejected += fileCompileRejected
		inputRejected += fileInputRejected
		falseAccepted += fileFalseAccepted
		falseRejected += fileFalseRejected
		t.Logf("public-suite file=%s checked=%d compile_rejected=%d input_rejected=%d false_accepted=%d false_rejected=%d mismatch_indexes=%s rejected_input_indexes=%s",
			name, fileChecked, fileCompileRejected, fileInputRejected, fileFalseAccepted, fileFalseRejected,
			strings.Join(mismatches, ","), strings.Join(rejectedInputs, ","))
	}
	if total < 100 || valid == 0 || invalid == 0 {
		t.Fatal("public suite positive and negative controls were incomplete")
	}
	if falseAccepted != 0 {
		t.Error("the selected public cases include a false acceptance")
	}
	t.Logf("public-suite total_checked=%d expected_valid=%d expected_invalid=%d compile_rejected=%d input_rejected=%d false_accepted=%d false_rejected=%d",
		total, valid, invalid, compileRejected, inputRejected, falseAccepted, falseRejected)
}
