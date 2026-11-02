//go:build darwin && cgo

// This is an isolated experiment, not the running ingress implementation.
package main

/*
#cgo LDFLAGS: -lproc
#include <libproc.h>
#include <string.h>
#include <sys/proc_info.h>

_Static_assert(sizeof(((struct proc_bsdinfo *)0)->pbi_start_tvsec) == 8 &&
               sizeof(((struct proc_bsdinfo *)0)->pbi_start_tvusec) == 8,
               "unexpected process birth-time size");

static int fixture_identity(int pid, unsigned char *birth, char *path, unsigned int path_len) {
    struct proc_bsdinfo info = {0};
    int n = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info));
    if (n != sizeof(info)) return 0;
    memcpy(birth, &info.pbi_start_tvsec, sizeof(info.pbi_start_tvsec));
    memcpy(birth + sizeof(info.pbi_start_tvsec), &info.pbi_start_tvusec, sizeof(info.pbi_start_tvusec));
    return proc_pidpath(pid, path, path_len);
}
*/
import "C"

import (
	"encoding/hex"
	"fmt"
	"unsafe"
)

func identity(pid int) (string, error) {
	var birth [16]byte
	var path [4096]byte
	n := C.fixture_identity(C.int(pid), (*C.uchar)(unsafe.Pointer(&birth[0])),
		(*C.char)(unsafe.Pointer(&path[0])), C.uint(len(path)))
	if n <= 0 || int(n) >= len(path) {
		return "", fmt.Errorf("identity unavailable")
	}
	return stamp("Darwin", pid, hex.EncodeToString(birth[:]), string(path[:int(n)])), nil
}
