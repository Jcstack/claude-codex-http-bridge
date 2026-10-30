package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

func certificateSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
}

func writePEM(root, name, kind string, der []byte) error {
	path := filepath.Join(root, name)
	if info, err := os.Lstat(path); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return errors.New("certificate path is symlinked")
	}
	file, err := os.CreateTemp(root, ".certificate-*")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if err = pem.Encode(file, &pem.Block{Type: kind, Bytes: der}); err != nil {
		file.Close()
		return err
	}
	if err = file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

func initializeTLS(root string) error {
	if err := checkPrivateRoot(root); err != nil {
		return err
	}
	caPath, keyPath := filepath.Join(root, "ca.pem"), filepath.Join(root, "ca-key.pem")
	_, certErr := os.Lstat(caPath)
	_, keyErr := os.Lstat(keyPath)
	now := time.Now()
	if os.IsNotExist(certErr) && os.IsNotExist(keyErr) {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return err
		}
		serial, err := certificateSerial()
		if err != nil {
			return err
		}
		ca := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "Claude Codex local runtime CA"},
			NotBefore: now.Add(-time.Hour), NotAfter: now.AddDate(10, 0, 0), IsCA: true, BasicConstraintsValid: true,
			KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
		der, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
		if err != nil {
			return err
		}
		keyDER, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			return err
		}
		if err = writePEM(root, "ca-key.pem", "PRIVATE KEY", keyDER); err != nil {
			return err
		}
		if err = writePEM(root, "ca.pem", "CERTIFICATE", der); err != nil {
			return err
		}
	} else if certErr != nil || keyErr != nil {
		return errors.New("incomplete local CA; refusing automatic identity replacement")
	}
	caPEM, err := privateFile(root, caPath, 65536)
	if err != nil {
		return err
	}
	keyPEM, err := privateFile(root, keyPath, 65536)
	if err != nil {
		return err
	}
	pair, err := tls.X509KeyPair(caPEM, keyPEM)
	if err != nil {
		return errors.New("invalid local CA pair")
	}
	ca, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil || !ca.IsCA || now.Before(ca.NotBefore) || ca.NotAfter.Before(now.AddDate(0, 2, 0)) {
		return errors.New("local CA invalid or nearing expiry; explicit identity migration required")
	}
	pool := x509.NewCertPool()
	pool.AddCert(ca)
	if leaf, loadErr := loadRuntimeCertificate(root); loadErr == nil {
		if cert, parseErr := x509.ParseCertificate(leaf.Certificate[0]); parseErr == nil {
			if _, verifyErr := cert.Verify(x509.VerifyOptions{Roots: pool, DNSName: "127.0.0.1", CurrentTime: now.AddDate(0, 0, 14)}); verifyErr == nil {
				return nil
			}
		}
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	serial, err := certificateSerial()
	if err != nil {
		return err
	}
	leaf := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "127.0.0.1"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.AddDate(1, 0, 0), IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
		DNSNames: []string{"localhost"}, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	if leaf.NotAfter.After(ca.NotAfter) {
		leaf.NotAfter = ca.NotAfter
	}
	der, err := x509.CreateCertificate(rand.Reader, leaf, ca, &key.PublicKey, pair.PrivateKey)
	if err != nil {
		return err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	if err = writePEM(root, "server-key.pem", "PRIVATE KEY", keyDER); err != nil {
		return err
	}
	return writePEM(root, "server.pem", "CERTIFICATE", der)
}

func loadRuntimeCertificate(root string) (*tls.Certificate, error) {
	cert, err := privateFile(root, filepath.Join(root, "server.pem"), 65536)
	if err != nil {
		return nil, err
	}
	key, err := privateFile(root, filepath.Join(root, "server-key.pem"), 65536)
	if err != nil {
		return nil, err
	}
	pair, err := tls.X509KeyPair(cert, key)
	return &pair, err
}

func runtimeCertificateProvider(root string) (func(*tls.ClientHelloInfo) (*tls.Certificate, error), error) {
	current, err := loadRuntimeCertificate(root)
	if err != nil {
		return nil, err
	}
	var mu sync.Mutex
	return func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
		mu.Lock()
		defer mu.Unlock()
		// Rotation replaces two files. Keep the previous valid pair if a
		// handshake arrives between those atomic writes.
		if fresh, loadErr := loadRuntimeCertificate(root); loadErr == nil {
			current = fresh
		}
		return current, nil
	}, nil
}
