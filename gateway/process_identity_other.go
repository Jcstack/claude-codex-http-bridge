//go:build !darwin && !linux && !windows

package main

import "errors"

func identityAvailable() bool      { return false }
func identity(int) (string, error) { return "", errors.New("native process identity is unavailable") }
