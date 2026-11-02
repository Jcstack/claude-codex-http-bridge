package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"path/filepath"
	"regexp"
	"strconv"
)

type originIdentity struct {
	Generation string `json:"generation"`
	PID        int    `json:"pid"`
	Port       int    `json:"port"`
	Identity   string `json:"identity"`
}

var identityPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func stamp(system string, pid int, birth, executable string) string {
	material := system + "\x00" + strconv.Itoa(pid) + "\x00" + birth + "\x00" + executable
	hash := sha256.Sum256([]byte(material))
	return hex.EncodeToString(hash[:])
}

func (g *gateway) checkOriginIdentity(cfg backendConfig) error {
	data, err := privateFile(g.root, filepath.Join(g.root, "owner-"+cfg.Generation+".json"), 4096)
	if err != nil {
		return errors.New("origin identity unavailable")
	}
	var owner originIdentity
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&owner) != nil || decoder.Decode(new(any)) != io.EOF ||
		owner.Generation != cfg.Generation || owner.PID != cfg.PID || owner.Port != cfg.Port ||
		!identityPattern.MatchString(owner.Identity) {
		return errors.New("invalid origin identity")
	}
	actual, err := identity(cfg.PID)
	if err != nil || subtle.ConstantTimeCompare([]byte(actual), []byte(owner.Identity)) != 1 {
		return errors.New("origin identity changed")
	}
	return nil
}
