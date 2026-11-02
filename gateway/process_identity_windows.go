package main

import (
	"fmt"
	"runtime"
	"strconv"
	"syscall"
	"unsafe"
)

var kernel = syscall.NewLazyDLL("kernel32.dll")
var queryImage = kernel.NewProc("QueryFullProcessImageNameW")
var mapString = kernel.NewProc("LCMapStringEx")

func identityAvailable() bool { return true }

func identity(pid int) (string, error) {
	if pid < 1 || uint64(pid) > 1<<32-1 {
		return "", fmt.Errorf("identity unavailable")
	}
	process, err := syscall.OpenProcess(0x1000, false, uint32(pid))
	if err != nil {
		return "", fmt.Errorf("identity unavailable")
	}
	defer syscall.CloseHandle(process)
	var creation, exit, kernelTime, userTime syscall.Filetime
	if syscall.GetProcessTimes(process, &creation, &exit, &kernelTime, &userTime) != nil {
		return "", fmt.Errorf("identity unavailable")
	}
	path := make([]uint16, 32768)
	size := uint32(len(path))
	ok, _, _ := queryImage.Call(uintptr(process), 0, uintptr(unsafe.Pointer(&path[0])), uintptr(unsafe.Pointer(&size)))
	runtime.KeepAlive(path)
	if ok == 0 || size == 0 || int(size) > len(path) {
		return "", fmt.Errorf("identity unavailable")
	}
	path = path[:size]
	for i := range path {
		if path[i] == '/' {
			path[i] = '\\'
		}
	}
	locale := []uint16{0} // LOCALE_NAME_INVARIANT
	const lowerCase = 0x00000100
	needed, _, _ := mapString.Call(uintptr(unsafe.Pointer(&locale[0])), lowerCase,
		uintptr(unsafe.Pointer(&path[0])), uintptr(len(path)), 0, 0, 0, 0, 0)
	runtime.KeepAlive(locale)
	runtime.KeepAlive(path)
	if needed == 0 || needed > 65536 {
		return "", fmt.Errorf("identity unavailable")
	}
	normalized := make([]uint16, int(needed))
	written, _, _ := mapString.Call(uintptr(unsafe.Pointer(&locale[0])), lowerCase,
		uintptr(unsafe.Pointer(&path[0])), uintptr(len(path)), uintptr(unsafe.Pointer(&normalized[0])), needed, 0, 0, 0)
	runtime.KeepAlive(locale)
	runtime.KeepAlive(path)
	runtime.KeepAlive(normalized)
	if written != needed {
		return "", fmt.Errorf("identity unavailable")
	}
	birth := uint64(creation.LowDateTime) | uint64(creation.HighDateTime)<<32
	return stamp("Windows", pid, strconv.FormatUint(birth, 10), syscall.UTF16ToString(normalized)), nil
}
