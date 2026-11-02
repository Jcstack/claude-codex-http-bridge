package main

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

var bootPattern = regexp.MustCompile(`^[a-fA-F0-9-]{36}$`)
var ticksPattern = regexp.MustCompile(`^[0-9]+$`)

func identityAvailable() bool { return true }

func identity(pid int) (string, error) {
	if pid < 1 || int64(pid) > 1<<31-1 {
		return "", fmt.Errorf("identity unavailable")
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return "", fmt.Errorf("identity unavailable")
	}
	bootID := strings.TrimSpace(string(boot))
	if !bootPattern.MatchString(bootID) {
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
	if len(fields) <= 19 || !ticksPattern.MatchString(fields[19]) {
		return "", fmt.Errorf("identity unavailable")
	}
	executable, err := os.Readlink(base + "/exe")
	if err != nil || executable == "" {
		return "", fmt.Errorf("identity unavailable")
	}
	executable = strings.TrimSuffix(executable, " (deleted)")
	if executable == "" {
		return "", fmt.Errorf("identity unavailable")
	}
	return stamp("Linux", pid, strings.ToLower(bootID)+":"+fields[19], executable), nil
}
