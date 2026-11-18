package database

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/dgraph-io/badger/v4"
)

func TestSessionPersistence(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	client, err := NewClient(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if session, err := client.GetSession(); session != nil || !errors.Is(err, ErrSessionNotFound) {
		t.Fatalf("expected missing session, got error %v", err)
	}
	want := auth.Session{Username: "PLAYER", Key: [40]byte{1, 2, 3}, AccountFlags: 0x00800000}
	if err := client.SaveSession(&want); err != nil {
		t.Fatal(err)
	}
	assertSessionStorageFields(t, client)
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	client, err = NewClient(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	got, err := client.GetSession()
	if err != nil || got == nil || *got != want {
		t.Fatalf("session did not survive reopen: %v", err)
	}
	want.Username = "OTHER"
	want.Key[39] = 255
	if err := client.SaveSession(&want); err != nil {
		t.Fatal(err)
	}
	got, err = client.GetSession()
	if err != nil || got == nil || *got != want {
		t.Fatalf("session was not replaced: %v", err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0700 {
		t.Errorf("database directory mode = %o, want 0700", info.Mode().Perm())
	}
}

func TestInvalidSessionPreservesSavedSession(t *testing.T) {
	client, err := NewClient(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	want := auth.Session{Username: "PLAYER", Key: [40]byte{1}}
	if err := client.SaveSession(&want); err != nil {
		t.Fatal(err)
	}
	for _, invalid := range []*auth.Session{
		nil,
		{},
		{Username: "PLAYER"},
		{Username: "PLAYER\n", Key: want.Key},
	} {
		if err := client.SaveSession(invalid); err == nil {
			t.Fatal("saved an invalid session")
		}
		got, err := client.GetSession()
		if err != nil || got == nil || *got != want {
			t.Fatalf("invalid save replaced existing session: %v", err)
		}
	}
}

func TestCorruptSession(t *testing.T) {
	client, err := NewClient(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	for _, data := range []string{
		`{"key":"secret-sentinel"`,
		`{"username": "PLAYER", "key":"AQID"}`,
		`null`,
		`{}`,
	} {
		if err := client.db.Update(func(txn *badger.Txn) error {
			return txn.Set([]byte(sessionKey), []byte(data))
		}); err != nil {
			t.Fatal(err)
		}
		got, err := client.GetSession()
		if got != nil || err == nil || errors.Is(err, ErrSessionNotFound) {
			t.Fatalf("expected corrupt session error, got %v", err)
		}
		if strings.Contains(err.Error(), "secret-sentinel") {
			t.Fatal("error leaked saved data")
		}
	}
}

func TestDatabaseErrors(t *testing.T) {
	if client, err := NewClient(""); client != nil || err == nil {
		t.Fatal("accepted empty database path")
	}
	dir := t.TempDir()
	file := filepath.Join(dir, "file")
	if err := os.WriteFile(file, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if client, err := NewClient(file); client != nil || err == nil {
		t.Fatal("opened file as database directory")
	}
	client, err := NewClient(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if second, err := NewClient(dir); err == nil {
		second.Close()
		t.Fatal("opened the same database twice")
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetSession(); err == nil || errors.Is(err, ErrSessionNotFound) {
		t.Fatal("closed database reported a missing session")
	}
	if err := client.SaveSession(&auth.Session{Username: "PLAYER", Key: [40]byte{1}}); err == nil {
		t.Fatal("saved to a closed database")
	}
}

func TestLegacySession(t *testing.T) {
	client, err := NewClient(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	want := auth.Session{Username: "PLAYER", Key: [40]byte{1, 2, 3}, AccountFlags: 0x00800000}
	data, err := json.Marshal(map[string]any{
		"username": want.Username, "key": want.Key[:], "account_flags": want.AccountFlags,
		"auth_server": "old.example:3724",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := client.db.Update(func(txn *badger.Txn) error {
		return txn.Set([]byte(sessionKey), data)
	}); err != nil {
		t.Fatal(err)
	}
	got, err := client.GetSession()
	if err != nil || got == nil || *got != want {
		t.Fatalf("legacy session did not load: %v", err)
	}
	if err := client.SaveSession(got); err != nil {
		t.Fatal(err)
	}
	assertSessionStorageFields(t, client)
}

func assertSessionStorageFields(t *testing.T, client *Client) {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := client.db.View(func(txn *badger.Txn) error {
		item, err := txn.Get([]byte(sessionKey))
		if err != nil {
			return err
		}
		return item.Value(func(data []byte) error { return json.Unmarshal(data, &fields) })
	}); err != nil {
		t.Fatal(err)
	}
	if len(fields) != 3 || fields["username"] == nil || fields["key"] == nil || fields["account_flags"] == nil {
		t.Fatal("saved session must contain only username, key and account_flags")
	}
}
