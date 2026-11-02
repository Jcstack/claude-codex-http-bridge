package probe

import (
	"context"
	"errors"
	"sort"
	"strings"
	"testing"
	"time"
)

// This fixture models a narrow stream gate; it is not a Responses or Messages
// parser and does not connect to a real model, translator, client or tool.
// Only a deliberately small, exact schema subset is accepted.
const syntheticMaxCalls, syntheticMaxArgumentBytes = 8, 16384

type syntheticTool struct{ name, schema string }
type syntheticItem struct {
	index                           int
	itemID, callID, name, arguments string
}
type syntheticCall struct {
	syntheticItem
	arguments strings.Builder
	done      bool
}

type syntheticGate struct {
	tools     map[string]string
	calls     map[int]*syntheticCall
	itemIDs   map[string]bool
	callIDs   map[string]bool
	pool      syntheticWorkerPool
	failed    bool
	completed bool
	emitted   []syntheticItem
}

func onlyKeys(value map[string]any, keys ...string) bool {
	if len(value) != len(keys) {
		return false
	}
	for _, key := range keys {
		if _, ok := value[key]; !ok {
			return false
		}
	}
	return true
}

func syntheticStringObjectSchema(raw string) bool {
	value, err := boundedJSON(raw)
	if err != nil {
		return false
	}
	schema, ok := value.(map[string]any)
	if !ok || !onlyKeys(schema, "type", "properties", "required", "additionalProperties") ||
		schema["type"] != "object" || schema["additionalProperties"] != false {
		return false
	}
	properties, ok := schema["properties"].(map[string]any)
	if !ok || len(properties) > 32 {
		return false
	}
	for _, property := range properties {
		field, ok := property.(map[string]any)
		if !ok || !onlyKeys(field, "type") || field["type"] != "string" {
			return false
		}
	}
	required, ok := schema["required"].([]any)
	if !ok || len(required) != len(properties) {
		return false
	}
	seen := make(map[string]bool)
	for _, item := range required {
		name, ok := item.(string)
		if !ok || seen[name] {
			return false
		}
		if _, ok := properties[name]; !ok {
			return false
		}
		seen[name] = true
	}
	return true
}

func newSyntheticGate(tools []syntheticTool, home string) (*syntheticGate, error) {
	if len(tools) == 0 || len(tools) > 32 {
		return nil, errors.New("unsupported fixture")
	}
	gate := &syntheticGate{tools: make(map[string]string), calls: make(map[int]*syntheticCall),
		itemIDs: make(map[string]bool), callIDs: make(map[string]bool),
		pool: syntheticWorkerPool{active: make(chan struct{}, 1), home: home}}
	for _, tool := range tools {
		if tool.name == "" || len(tool.name) > 256 || gate.tools[tool.name] != "" || !syntheticStringObjectSchema(tool.schema) {
			return nil, errors.New("unsupported fixture")
		}
		gate.tools[tool.name] = tool.schema
	}
	return gate, nil
}

func (gate *syntheticGate) reject() error {
	gate.failed = true
	return errors.New("synthetic tool stream rejected")
}

func (gate *syntheticGate) add(index int, itemID, callID, name string) error {
	if gate.failed || gate.completed || index < 0 || len(gate.calls) >= syntheticMaxCalls ||
		itemID == "" || callID == "" || len(itemID) > 256 || len(callID) > 256 || gate.calls[index] != nil || gate.itemIDs[itemID] ||
		gate.callIDs[callID] || gate.tools[name] == "" {
		return gate.reject()
	}
	gate.calls[index] = &syntheticCall{syntheticItem: syntheticItem{index: index, itemID: itemID, callID: callID, name: name}}
	gate.itemIDs[itemID], gate.callIDs[callID] = true, true
	return nil
}

func (gate *syntheticGate) delta(index int, itemID, part string) error {
	call := gate.calls[index]
	if gate.failed || gate.completed || call == nil || call.done || call.itemID != itemID ||
		len(part) > syntheticMaxArgumentBytes-call.arguments.Len() {
		return gate.reject()
	}
	call.arguments.WriteString(part)
	return nil
}

func (gate *syntheticGate) done(item syntheticItem) error {
	call := gate.calls[item.index]
	if gate.failed || gate.completed || call == nil || call.done || call.itemID != item.itemID ||
		call.callID != item.callID || call.name != item.name || call.arguments.String() != item.arguments {
		return gate.reject()
	}
	call.done = true
	return nil
}

func (gate *syntheticGate) complete(ctx context.Context, items []syntheticItem) error {
	if gate.failed || gate.completed || len(items) != len(gate.calls) || len(items) == 0 {
		return gate.reject()
	}
	if _, ok := ctx.Deadline(); !ok || ctx.Err() != nil {
		return gate.reject()
	}
	seen := make(map[int]bool)
	ordered := append([]syntheticItem(nil), items...)
	for _, item := range ordered {
		call := gate.calls[item.index]
		if seen[item.index] || call == nil || !call.done || call.itemID != item.itemID ||
			call.callID != item.callID || call.name != item.name || call.arguments.String() != item.arguments {
			return gate.reject()
		}
		seen[item.index] = true
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].index < ordered[j].index })
	for _, item := range ordered {
		result, err := gate.pool.run(ctx, "validate", syntheticWorkerRequest{
			Schema: gate.tools[item.name], Input: item.arguments}, nil)
		if err != nil || result != "valid" {
			return gate.reject()
		}
	}
	if ctx.Err() != nil {
		return gate.reject()
	}
	gate.completed = true
	gate.emitted = ordered
	return nil
}

const syntheticSafeSchema = `{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}`

func newTestGate(t *testing.T) *syntheticGate {
	t.Helper()
	gate, err := newSyntheticGate([]syntheticTool{{name: "fixture", schema: syntheticSafeSchema}}, t.TempDir())
	if err != nil {
		t.Fatal("synthetic gate setup failed")
	}
	return gate
}

func fixtureContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func appendFixtureCall(t *testing.T, gate *syntheticGate, item syntheticItem) {
	t.Helper()
	if gate.add(item.index, item.itemID, item.callID, item.name) != nil ||
		gate.delta(item.index, item.itemID, item.arguments) != nil || gate.done(item) != nil {
		t.Fatal("synthetic call setup failed")
	}
}

func TestSyntheticGateInterleavedCalls(t *testing.T) {
	gate := newTestGate(t)
	a := syntheticItem{index: 0, itemID: "item-a", callID: "call-a", name: "fixture", arguments: `{"value":"a"}`}
	b := syntheticItem{index: 1, itemID: "item-b", callID: "call-b", name: "fixture", arguments: `{"value":"b"}`}
	if gate.add(a.index, a.itemID, a.callID, a.name) != nil || gate.add(b.index, b.itemID, b.callID, b.name) != nil ||
		gate.delta(a.index, a.itemID, a.arguments[:5]) != nil || gate.delta(b.index, b.itemID, b.arguments) != nil ||
		gate.delta(a.index, a.itemID, a.arguments[5:]) != nil || gate.done(b) != nil || gate.done(a) != nil {
		t.Fatal("interleaved fixture was rejected")
	}
	if len(gate.emitted) != 0 {
		t.Fatal("a synthetic tool was released before the terminal event")
	}
	if gate.complete(fixtureContext(t), []syntheticItem{b, a}) != nil || len(gate.emitted) != 2 ||
		gate.emitted[0].callID != a.callID || gate.emitted[1].callID != b.callID {
		t.Fatal("valid synthetic tools were not released in index order")
	}
	if gate.complete(fixtureContext(t), []syntheticItem{a, b}) == nil || len(gate.emitted) != 2 {
		t.Fatal("repeated completion was accepted")
	}
}

func TestSyntheticGateRejectsInvalidParallelCall(t *testing.T) {
	for _, arguments := range []string{`{"value":123}`, `{"value":"ok","extra":"x"}`,
		`{"value":"a","value":"b"}`, `{"value":`} {
		gate := newTestGate(t)
		a := syntheticItem{index: 0, itemID: "item-a", callID: "call-a", name: "fixture", arguments: `{"value":"ok"}`}
		b := syntheticItem{index: 1, itemID: "item-b", callID: "call-b", name: "fixture", arguments: arguments}
		appendFixtureCall(t, gate, a)
		appendFixtureCall(t, gate, b)
		if gate.complete(fixtureContext(t), []syntheticItem{a, b}) == nil || len(gate.emitted) != 0 {
			t.Fatal("invalid parallel call released a synthetic tool")
		}
	}
}

func TestSyntheticGateRejectsIdentityAndTerminalChanges(t *testing.T) {
	item := syntheticItem{index: 0, itemID: "item-a", callID: "call-a", name: "fixture", arguments: `{"value":"ok"}`}
	for _, mode := range []string{"unknown", "duplicate-call", "duplicate-item", "delta-item", "delta-after-done", "done-arguments", "incomplete-call", "terminal-id", "missing-terminal"} {
		t.Run(mode, func(t *testing.T) {
			gate := newTestGate(t)
			if mode == "unknown" {
				if gate.add(0, "item-a", "call-a", "unknown") == nil {
					t.Fatal("unknown synthetic tool was accepted")
				}
			} else if mode == "duplicate-call" {
				if gate.add(0, "item-a", "call-a", "fixture") != nil ||
					gate.add(1, "item-b", "call-a", "fixture") == nil {
					t.Fatal("duplicate synthetic call identity was accepted")
				}
			} else if mode == "duplicate-item" {
				if gate.add(0, "item-a", "call-a", "fixture") != nil ||
					gate.add(1, "item-a", "call-b", "fixture") == nil {
					t.Fatal("duplicate synthetic item identity was accepted")
				}
			} else if mode == "delta-item" {
				if gate.add(0, item.itemID, item.callID, item.name) != nil || gate.delta(0, "other", item.arguments) == nil {
					t.Fatal("changed synthetic item identity was accepted")
				}
			} else if mode == "delta-after-done" {
				appendFixtureCall(t, gate, item)
				if gate.delta(0, item.itemID, " ") == nil {
					t.Fatal("a late synthetic argument delta was accepted")
				}
			} else if mode == "done-arguments" {
				if gate.add(0, item.itemID, item.callID, item.name) != nil || gate.delta(0, item.itemID, item.arguments) != nil {
					t.Fatal("synthetic setup failed")
				}
				changed := item
				changed.arguments = `{"value":"changed"}`
				if gate.done(changed) == nil {
					t.Fatal("changed synthetic arguments were accepted")
				}
			} else if mode == "incomplete-call" {
				if gate.add(0, item.itemID, item.callID, item.name) != nil || gate.delta(0, item.itemID, item.arguments) != nil ||
					gate.complete(fixtureContext(t), []syntheticItem{item}) == nil {
					t.Fatal("unfinished synthetic call was accepted")
				}
			} else {
				appendFixtureCall(t, gate, item)
				terminal := item
				terminal.callID = "other"
				items := []syntheticItem{terminal}
				if mode == "missing-terminal" {
					items = nil
				}
				if gate.complete(fixtureContext(t), items) == nil {
					t.Fatal("changed or missing synthetic terminal call was accepted")
				}
			}
			if len(gate.emitted) != 0 {
				t.Fatal("rejected synthetic call was released")
			}
		})
	}
}

func TestSyntheticGateCancellationAndLimits(t *testing.T) {
	item := syntheticItem{index: 0, itemID: "item-a", callID: "call-a", name: "fixture", arguments: `{"value":"ok"}`}
	gate := newTestGate(t)
	appendFixtureCall(t, gate, item)
	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(time.Second))
	cancel()
	if gate.complete(ctx, []syntheticItem{item}) == nil || len(gate.emitted) != 0 {
		t.Fatal("canceled synthetic call was released")
	}
	gate = newTestGate(t)
	appendFixtureCall(t, gate, item)
	if gate.complete(context.Background(), []syntheticItem{item}) == nil || len(gate.emitted) != 0 {
		t.Fatal("synthetic validation without a deadline was accepted")
	}
	gate = newTestGate(t)
	if gate.add(0, item.itemID, item.callID, item.name) != nil ||
		gate.delta(0, item.itemID, strings.Repeat("x", syntheticMaxArgumentBytes+1)) == nil || len(gate.emitted) != 0 {
		t.Fatal("oversized synthetic arguments were accepted")
	}
	for _, schema := range []string{`{"type":"object","properties":{"value":{"type":"string","pattern":"^.$"}},"required":["value"],"additionalProperties":false}`,
		`{"type":"object","properties":{"value":{"type":"string","format":"unknown"}},"required":["value"],"additionalProperties":false}`,
		`{"type":"object","properties":{"value":{"type":"string"}},"required":[],"additionalProperties":false}`} {
		if _, err := newSyntheticGate([]syntheticTool{{name: "fixture", schema: schema}}, t.TempDir()); err == nil {
			t.Fatal("unsupported synthetic schema was accepted")
		}
	}
}

func TestSyntheticGateRejectsCrossRequestCall(t *testing.T) {
	a, b := newTestGate(t), newTestGate(t)
	first := syntheticItem{index: 0, itemID: "item-a", callID: "call-a", name: "fixture", arguments: `{"value":"ok"}`}
	second := syntheticItem{index: 0, itemID: "item-b", callID: "call-b", name: "fixture", arguments: `{"value":"ok"}`}
	appendFixtureCall(t, a, first)
	appendFixtureCall(t, b, second)
	if b.complete(fixtureContext(t), []syntheticItem{first}) == nil || len(b.emitted) != 0 || len(a.emitted) != 0 {
		t.Fatal("a call from another synthetic request was accepted")
	}
}
