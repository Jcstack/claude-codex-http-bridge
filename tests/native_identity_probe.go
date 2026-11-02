// This program only probes private fixture processes; it is not the ingress.
package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"time"
)

func stamp(system string, pid int, birth, executable string) string {
	material := system + "\x00" + strconv.Itoa(pid) + "\x00" + birth + "\x00" + executable
	hash := sha256.Sum256([]byte(material))
	return hex.EncodeToString(hash[:])
}

func matches(pid int, expected string) bool {
	actual, err := identity(pid)
	return err == nil && subtle.ConstantTimeCompare([]byte(actual), []byte(expected)) == 1
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--fixture-child" {
		fmt.Println("READY")
		time.Sleep(30 * time.Second)
		return
	}
	if len(os.Args) != 3 {
		os.Exit(2)
	}
	pid, err := strconv.Atoi(os.Args[1])
	if err != nil || pid < 1 {
		os.Exit(2)
	}
	const checksPerSample = 32
	samples := make([]float64, 100)
	for i := range samples {
		start := time.Now()
		for j := 0; j < checksPerSample; j++ {
			if !matches(pid, os.Args[2]) {
				os.Exit(1)
			}
		}
		samples[i] = float64(time.Since(start).Nanoseconds()) / 1e6 / checksPerSample
	}
	sort.Float64s(samples)
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
		"synthetic_local_process_only": true, "native_matches_python_identity": true,
		"samples": len(samples), "checks_per_sample": checksPerSample, "milliseconds_per_check": map[string]float64{
			"median": samples[len(samples)/2], "p95": samples[len(samples)*95/100]},
	})
}
