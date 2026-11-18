package credentials

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCredentialsFile(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	path := filepath.Join(dir, "auth.json")
	want := Credentials{Username: "player", Password: " password \"é "}
	if err := Write(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := Read(path)
	if err != nil || got != want {
		t.Fatalf("credentials did not round-trip: %v", err)
	}
	for path, mode := range map[string]os.FileMode{dir: 0700, path: 0600} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s has mode %o, want %o", path, info.Mode().Perm(), mode)
		}
	}
	// Reinitializing replaces the file, including any formerly loose mode.
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	want.Password = "replacement"
	if err := Write(path, want); err != nil {
		t.Fatal(err)
	}
	got, err = Read(path)
	if err != nil || got != want {
		t.Fatal("credentials were not replaced")
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("replacement auth file is not private")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("temporary credential files were left behind")
	}
}

func TestInvalidCredentialsFiles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth.json")
	if _, err := Read(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing-file error lost: %v", err)
	}
	for _, data := range []string{
		`{`, `null`, `[]`, `{}`, `{"username":"player"}`,
		`{"username":"player","password":123}`,
		`{"username":"player","password":"secret"} {}`,
		`{"username":"","password":"secret"}`,
		strings.Repeat("x", 65537),
	} {
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		credentials, err := Read(path)
		if err == nil || credentials != (Credentials{}) {
			t.Fatal("accepted invalid credentials file")
		}
		if strings.Contains(err.Error(), "secret") {
			t.Fatal("invalid auth file error exposed its contents")
		}
	}
}

func TestCredentialsSaveFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "auth.json")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := Write(path, Credentials{Username: "player", Password: "secret"}); err == nil {
		t.Fatal("expected error replacing a directory")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("failed save left temporary credentials behind")
	}
}
