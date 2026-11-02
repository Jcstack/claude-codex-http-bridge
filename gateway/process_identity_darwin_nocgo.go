//go:build darwin && !cgo

package main

import "errors"

func identityAvailable() bool      { return false }
func identity(int) (string, error) { return "", errors.New("native process identity requires cgo") }
