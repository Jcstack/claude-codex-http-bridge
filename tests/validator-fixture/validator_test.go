package probe

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"testing"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"
)

// These are fixture limits, not a recommended production configuration.
const maxBytes, maxDepth, maxTokens, maxNumberBytes, maxExponent = 65536, 32, 4096, 64, 64

func boundedJSON(raw string) (any, error) {
	if len(raw) > maxBytes {
		return nil, errors.New("limit")
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
