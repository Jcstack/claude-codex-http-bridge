// This is an isolated experiment, not the running ingress implementation.
package main

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

func identity(pid int) (string, error) {
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return "", fmt.Errorf("identity unavailable")
	}
	bootID := strings.TrimSpace(string(boot))
	if !regexp.MustCompile(`^[a-fA-F0-9-]{36}$`).MatchString(bootID) {
		return "", fmt.Errorf("identity unavailable")
	}
	base := "/proc/" + strconv.Itoa(pid)
	stat, err := os.ReadFile(base + "/stat")
	if err != nil {
		return "", fmt.Errorf("identity unavailable")
	}
	closing := strings.LastIndexByte(string(stat), ')')
	if closing < 0 {
		return "", fmt.Errorf("identity unavailable")
	}
	fields := strings.Fields(string(stat)[closing+1:])
	if len(fields) <= 19 || !regexp.MustCompile(`^[0-9]+$`).MatchString(fields[19]) {
		return "", fmt.Errorf("identity unavailable")
	}
	executable, err := os.Readlink(base + "/exe")
	if err != nil || executable == "" {
		return "", fmt.Errorf("identity unavailable")
	}
	executable = strings.TrimSuffix(executable, " (deleted)")
	return stamp("Linux", pid, strings.ToLower(bootID)+":"+fields[19], executable), nil
}
