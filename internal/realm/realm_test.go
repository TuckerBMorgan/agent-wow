package realm_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agent-wow/agent-wow/internal/realm"
)

func TestRealmFile(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "config")
	path := filepath.Join(dir, "realm.json")
	want := realm.Realm{ID: 7, Name: "AzerothCore", Address: "127.0.0.1:8085"}
	if err := realm.Write(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := realm.Read(path)
	if err != nil || got != want {
		t.Fatalf("selection did not round-trip: %v", err)
	}
	assertRealmStorageFields(t, path)
	for path, mode := range map[string]os.FileMode{dir: 0700, path: 0600} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s has mode %o, want %o", path, info.Mode().Perm(), mode)
		}
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	want.ID = 8
	if err := realm.Write(path, want); err != nil {
		t.Fatal(err)
	}
	got, err = realm.Read(path)
	if err != nil || got != want {
		t.Fatal("selection was not replaced")
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("replacement realm file is not private")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("temporary realm files were left behind")
	}
	// Each call uses its explicit path, without shared initialization or state.
	otherPath := filepath.Join(t.TempDir(), "other.json")
	other := realm.Realm{ID: 1, Name: "Other", Address: "other.example:8085"}
	if err := realm.Write(otherPath, other); err != nil {
		t.Fatal(err)
	}
	got, err = realm.Read(path)
	if err != nil || got != want {
		t.Fatal("writing another path changed the first selection")
	}
	got, err = realm.Read(otherPath)
	if err != nil || got != other {
		t.Fatal("second path did not retain its selection")
	}
}

func TestInvalidRealmPreservesSelection(t *testing.T) {
	path := filepath.Join(t.TempDir(), "realm.json")
	want := realm.Realm{ID: 1, Name: "Original", Address: "localhost:8085"}
	if err := realm.Write(path, want); err != nil {
		t.Fatal(err)
	}
	for _, selection := range []realm.Realm{
		{}, {ID: 2, Name: "Invalid\nName", Address: "localhost:8085"},
		{ID: 2, Name: "Invalid Address", Address: "localhost:0"},
	} {
		before, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := realm.Write(path, selection); err == nil {
			t.Fatal("accepted invalid selection")
		}
		after, err := os.ReadFile(path)
		if err != nil || string(before) != string(after) {
			t.Fatal("invalid selection changed the file")
		}
	}
}

func TestRealmWriteFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "realm.json")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := realm.Write(path, realm.Realm{ID: 1, Name: "Realm", Address: "localhost:8085"}); err == nil {
		t.Fatal("ignored failed rename")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("failed write left a temporary file")
	}
}

func TestInvalidRealmFiles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "realm.json")
	if _, err := realm.Read(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing-file error lost: %v", err)
	}
	for _, data := range []string{
		`{`, `null`, `[]`, `{}`, `{"id":256,"name":"Bad"}`,
		`{"id":7,"name":"Realm","address":123}`,
		`{"id":7,"name":"Realm","address":"localhost:0"}`,
		`{"id":7,"name":"Realm","address":"localhost:8085"} {}`,
		strings.Repeat("x", 65537),
	} {
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		got, err := realm.Read(path)
		if err == nil || got != (realm.Realm{}) {
			t.Fatal("accepted invalid realm file or returned partial selection")
		}
	}
}

func TestLegacyRealmFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "realm.json")
	data := []byte(`{"id":7,"name":"AzerothCore","address":"127.0.0.1:8085","authserver":"old.example:3724"}`)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	want := realm.Realm{ID: 7, Name: "AzerothCore", Address: "127.0.0.1:8085"}
	got, err := realm.Read(path)
	if err != nil || got != want {
		t.Fatalf("legacy realm file did not load: %v", err)
	}
	if err := realm.Write(path, got); err != nil {
		t.Fatal(err)
	}
	assertRealmStorageFields(t, path)
}

func assertRealmStorageFields(t *testing.T, path string) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	if len(fields) != 3 || fields["id"] == nil || fields["name"] == nil || fields["address"] == nil {
		t.Fatal("realm file must contain only id, name and address")
	}
}
