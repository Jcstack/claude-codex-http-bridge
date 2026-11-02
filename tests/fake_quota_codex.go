// Fake native Codex app-server for the disposable Windows quota fixture.
package main

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
)

func main() {
	if len(os.Args) != 2 || os.Args[1] != "app-server" {
		os.Exit(2)
	}
	executable, err := os.Executable()
	if err != nil {
		os.Exit(2)
	}
	expectedHome := filepath.Join(filepath.Dir(filepath.Dir(executable)), "native")
	_, apiKeyPresent := os.LookupEnv("OPENAI_API_KEY")
	_, pythonPathPresent := os.LookupEnv("PYTHONPATH")
	safe := !apiKeyPresent && !pythonPathPresent && os.Getenv("CODEX_HOME") == expectedHome &&
		os.Getenv("HTTPS_PROXY") == "http://proxy.invalid:1234"
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	encoder := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		var message struct {
			ID int `json:"id"`
		}
		if json.Unmarshal(scanner.Bytes(), &message) != nil {
			continue
		}
		result := map[string]any{}
		if message.ID == 2 {
			account := "wrong-account"
			if safe {
				account = "fixture-account"
			}
			result["accountId"] = account
			result["rateLimitsByLimitId"] = map[string]any{"codex": map[string]any{"secondary": map[string]any{
				"windowDurationMins": 10080, "usedPercent": 12.5, "resetsAt": 2000000000,
			}}}
		} else if message.ID != 1 {
			continue
		}
		if encoder.Encode(map[string]any{"id": message.ID, "result": result}) != nil {
			return
		}
	}
}
