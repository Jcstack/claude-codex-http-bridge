package probe

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"
)

// These are fixture limits, not a recommended production configuration.
const maxBytes, maxDepth, maxTokens, maxNumberBytes, maxExponent = 65536, 32, 4096, 64, 64

// encoding/json replaces malformed UTF-8 and lone escaped surrogates with a
// replacement rune. Refuse those lossy cases in this fixture before decoding.
func rejectLossyUnicode(raw string) error {
	if !utf8.ValidString(raw) {
		return errors.New("invalid unicode")
	}
	inString := false
	for i := 0; i < len(raw); i++ {
		if !inString {
			if raw[i] == '"' {
				inString = true
			}
			continue
		}
		if raw[i] == '"' {
			inString = false
			continue
		}
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return errors.New("invalid unicode")
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return errors.New("invalid unicode")
		}
		first, err := strconv.ParseUint(raw[i+1:i+5], 16, 16)
		if err != nil {
			return errors.New("invalid unicode")
		}
		i += 4
		if first >= 0xDC00 && first <= 0xDFFF {
			return errors.New("invalid unicode")
		}
		if first >= 0xD800 && first <= 0xDBFF {
			if i+6 >= len(raw) || raw[i+1:i+3] != `\u` {
				return errors.New("invalid unicode")
			}
			second, err := strconv.ParseUint(raw[i+3:i+7], 16, 16)
			if err != nil || second < 0xDC00 || second > 0xDFFF {
				return errors.New("invalid unicode")
			}
			i += 6
		}
	}
	return nil
}

func boundedJSON(raw string) (any, error) {
	if len(raw) > maxBytes {
		return nil, errors.New("limit")
	}
	if err := rejectLossyUnicode(raw); err != nil {
		return nil, err
	}
	d := json.NewDecoder(strings.NewReader(raw))
	d.UseNumber()
	depth, tokens := 0, 0
	var read func() (any, error)
	read = func() (any, error) {
		tokens++
		if tokens > maxTokens {
			return nil, errors.New("limit")
		}
		token, err := d.Token()
		if err != nil {
			return nil, errors.New("invalid")
		}
		if number, ok := token.(json.Number); ok {
			s := string(number)
			if len(s) > maxNumberBytes {
				return nil, errors.New("limit")
			}
			if i := strings.IndexAny(s, "eE"); i >= 0 {
				exponent, err := strconv.Atoi(s[i+1:])
				if err != nil || exponent > maxExponent || exponent < -maxExponent {
					return nil, errors.New("limit")
				}
			}
		}
		delim, isContainer := token.(json.Delim)
		if !isContainer {
			return token, nil
		}
		depth++
		if depth > maxDepth {
			return nil, errors.New("limit")
		}
		defer func() { depth-- }()
		switch delim {
		case '{':
			result := map[string]any{}
			for d.More() {
				keyToken, err := d.Token()
				if err != nil {
					return nil, errors.New("invalid")
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, errors.New("invalid")
				}
				if _, ok := result[key]; ok {
					return nil, errors.New("duplicate")
				}
				value, err := read()
				if err != nil {
					return nil, err
				}
				result[key] = value
			}
			end, err := d.Token()
			if err != nil || end != json.Delim('}') {
				return nil, errors.New("invalid")
			}
			return result, nil
		case '[':
			result := []any{}
			for d.More() {
				value, err := read()
				if err != nil {
					return nil, err
				}
				result = append(result, value)
			}
			end, err := d.Token()
			if err != nil || end != json.Delim(']') {
				return nil, errors.New("invalid")
			}
			return result, nil
		default:
			return nil, errors.New("invalid")
		}
	}
	result, err := read()
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); err != io.EOF {
		return nil, errors.New("invalid")
	}
	return result, nil
}

type denyLoader struct{ attempts int }

func (l *denyLoader) Load(_ string) (any, error) {
	l.attempts++
	return nil, errors.New("external disabled")
}

func compile(raw string, loader *denyLoader) (*jsonschema.Schema, error) {
	doc, err := boundedJSON(raw)
	if err != nil {
		return nil, errors.New("schema rejected")
	}
	c := jsonschema.NewCompiler()
	c.DefaultDraft(jsonschema.Draft2020)
	c.AssertFormat()
	c.UseLoader(loader)
	if err := c.AddResource("https://fixture.invalid/schema", doc); err != nil {
		return nil, errors.New("schema rejected")
	}
	schema, err := c.Compile("https://fixture.invalid/schema")
	if err != nil {
		return nil, errors.New("schema rejected")
	}
	return schema, nil
}

func validate(schema *jsonschema.Schema, raw string) error {
	value, err := boundedJSON(raw)
	if err != nil {
		return errors.New("input rejected")
	}
	if err := schema.Validate(value); err != nil {
		return errors.New("input rejected")
	}
	return nil
}

func TestFixtureTypeAndLargeIntegerPrecision(t *testing.T) {
	schema, err := compile(`{"type":"object","properties":{"value":{"type":"integer","const":9007199254740993}},"required":["value"],"additionalProperties":false}`, &denyLoader{})
	if err != nil {
		t.Fatal("fixture schema failed")
	}
	if validate(schema, `{"value":9007199254740993}`) != nil {
		t.Fatal("exact integer rejected")
	}
	if validate(schema, `{"value":9007199254740992}`) == nil {
		t.Fatal("different integer accepted")
	}
	if validate(schema, `{"value":"9007199254740993"}`) == nil {
		t.Fatal("numeric string accepted")
	}
	if validate(schema, `{"value":9007199254740993,"extra":1}`) == nil {
		t.Fatal("extra property accepted")
	}
}

func TestFixtureLocalAndExternalReferences(t *testing.T) {
	loader := &denyLoader{}
	schema, err := compile(`{"$defs":{"field":{"type":"string"}},"type":"object","properties":{"value":{"$ref":"#/$defs/field"}},"required":["value"]}`, loader)
	if err != nil || validate(schema, `{"value":"ok"}`) != nil || loader.attempts != 0 {
		t.Fatal("local reference fixture failed")
	}
	if validate(schema, `{"value":123}`) == nil {
		t.Fatal("local reference did not validate")
	}
	loader = &denyLoader{}
	if _, err := compile(`{"$ref":"https://external.invalid/schema"}`, loader); err == nil || loader.attempts == 0 {
		t.Fatal("external reference was not denied")
	}
}

func TestFixtureKnownFormatIsValidated(t *testing.T) {
	schema, err := compile(`{"type":"string","format":"ipv4"}`, &denyLoader{})
	if err != nil {
		t.Fatal("fixture schema failed")
	}
	if validate(schema, `"127.0.0.1"`) != nil {
		t.Fatal("valid IPv4 rejected")
	}
	if validate(schema, `"not-an-ip"`) == nil {
		t.Fatal("invalid IPv4 accepted")
	}
}

func TestFixtureUnknownFormatObservation(t *testing.T) {
	schema, err := compile(`{"type":"string","format":"unknown-synthetic-format"}`, &denyLoader{})
	if err == nil && validate(schema, `"anything"`) == nil {
		t.Log("unknown format was not enforced; production use requires an explicit policy")
	}
}

func TestFixtureRegexpDialectObservation(t *testing.T) {
	// The default Go regexp engine and an ECMAScript Unicode RegExp do not
	// agree on these synthetic line terminators. Observe a false acceptance;
	// this fixture must not be promoted to a production validation guard.
	schema, err := compile(`{"type":"string","pattern":"^.$"}`, &denyLoader{})
	if err != nil || validate(schema, `"a"`) != nil {
		t.Fatal("regexp positive control failed")
	}
	if validate(schema, `"\r"`) != nil || validate(schema, `"\u2028"`) != nil {
		t.Fatal("fixture regexp behavior changed; reassess the dialect observation")
	}
	node, err := exec.LookPath("node")
	if err != nil {
		if os.Getenv("BRIDGE_REQUIRE_NODE") == "1" {
			t.Fatal("Node is required by this observation gate")
		}
		t.Skip("Node is required for the separate ECMAScript control")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, node, "-e",
		`if (!/^.$/u.test("a") || /^.$/u.test("\r") || /^.$/u.test("\u2028")) process.exit(1)`)
	command.Env = []string{"HOME=" + t.TempDir()}
	for _, key := range []string{"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT"} {
		if value, ok := os.LookupEnv(key); ok {
			command.Env = append(command.Env, key+"="+value)
		}
	}
	if err := command.Run(); err != nil {
		t.Fatal("ECMAScript regexp control failed")
	}
	t.Log("default fixture regexp accepted two line terminators rejected by the ECMAScript control")
}

type syntheticRegexpFailure struct{}

func (syntheticRegexpFailure) MatchString(string) bool { return false }
func (syntheticRegexpFailure) String() string          { return "synthetic" }

func TestFixtureRegexpFailureAsNoMatchObservation(t *testing.T) {
	const raw = `{"not":{"pattern":"^a$"}}`
	baseline, err := compile(raw, &denyLoader{})
	if err != nil || validate(baseline, `"a"`) == nil {
		t.Fatal("regexp negation control failed")
	}
	doc, err := boundedJSON(raw)
	if err != nil {
		t.Fatal("fixture schema failed")
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	compiler.UseLoader(&denyLoader{})
	// Simulates an adapter that converts a match error into the boolean false.
	// It does not use regexp2 or establish a real timeout.
	compiler.UseRegexpEngine(func(string) (jsonschema.Regexp, error) { return syntheticRegexpFailure{}, nil })
	if err := compiler.AddResource("https://fixture.invalid/error", doc); err != nil {
		t.Fatal("fixture schema failed")
	}
	schema, err := compiler.Compile("https://fixture.invalid/error")
	if err != nil || validate(schema, `"a"`) != nil {
		t.Fatal("synthetic error-as-no-match observation changed")
	}
	t.Log("mapping a synthetic regexp error to false caused acceptance under not")
}

func TestFixtureBoundsAndDuplicateKeys(t *testing.T) {
	cases := []string{`{"x":1,"x":2}`, `1e999999999`, `0.` + strings.Repeat("1", maxNumberBytes),
		strings.Repeat("[", maxDepth+1) + `0` + strings.Repeat("]", maxDepth+1),
		`"` + strings.Repeat("x", maxBytes) + `"`}
	for i, raw := range cases {
		if _, err := boundedJSON(raw); err == nil {
			t.Fatalf("unbounded fixture %d accepted", i)
		}
	}
	tooMany := `[` + strings.Repeat("0,", maxTokens) + `0]`
	if _, err := boundedJSON(tooMany); err == nil {
		t.Fatal("token limit was not applied")
	}
}

func TestFixtureRejectsLossyUnicode(t *testing.T) {
	var defaultDecoded string
	if err := json.Unmarshal([]byte(`"\ud800"`), &defaultDecoded); err != nil || defaultDecoded != "\ufffd" {
		t.Fatal("standard decoder control changed; reassess lossy input handling")
	}
	for _, raw := range []string{`"\ud800"`, `"\udc00"`, `"\ud800\u0041"`,
		`{"\ud800":true}`, string([]byte{'"', 0xff, '"'})} {
		if _, err := boundedJSON(raw); err == nil {
			t.Fatal("lossy Unicode was accepted")
		}
	}
	for _, raw := range []string{`"\ud83d\ude00"`, `"😀"`, `"\ufffd"`, `"\\ud800"`} {
		if _, err := boundedJSON(raw); err != nil {
			t.Fatal("valid Unicode fixture was rejected")
		}
	}
	schema, err := compile(`{"const":"\ud800"}`, &denyLoader{})
	if err == nil || schema != nil {
		t.Fatal("a lossy schema was compiled")
	}
}

func TestFixtureErrorsDoNotContainPrivateInputs(t *testing.T) {
	schema, err := compile(`{"type":"string","maxLength":1}`, &denyLoader{})
	if err != nil {
		t.Fatal("fixture schema failed")
	}
	const sentinel = "PRIVATE_SYNTHETIC_SENTINEL"
	err = validate(schema, fmt.Sprintf("%q", sentinel))
	if err == nil || strings.Contains(err.Error(), sentinel) {
		t.Fatal("invalid value was accepted or exposed")
	}
}
