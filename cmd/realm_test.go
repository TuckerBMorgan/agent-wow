package cmd

import (
	"bytes"
	"crypto/sha1"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/internal/config"
	realmstore "github.com/agent-wow/agent-wow/internal/realm"
	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/database"
)

func TestRealmListJSON(t *testing.T) {
	for _, empty := range []bool{false, true} {
		t.Run(map[bool]string{false: "realms", true: "empty"}[empty], func(t *testing.T) {
			realms := []auth.Realm{}
			if !empty {
				realms = append(realms, auth.Realm{ID: 7, Name: "Live Realm", Address: "localhost:8085", Type: 1, Characters: 2, Population: 0.5})
			}
			serveCommandRealms(t, realms, false)
			if !empty {
				if err := saveRealm(realms[0]); err != nil {
					t.Fatal(err)
				}
			}
			command := newRealmCommand()
			command.SetArgs([]string{"list", "--json"})
			var out, stderr bytes.Buffer
			command.SetOut(&out)
			command.SetErr(&stderr)
			if err := command.Execute(); err != nil {
				t.Fatal(err)
			}
			var result struct {
				Realms []struct {
					Selected   bool   `json:"selected"`
					Type       string `json:"type"`
					Status     string `json:"status"`
					Characters uint8  `json:"characters"`
				} `json:"realms"`
			}
			if err := json.Unmarshal(out.Bytes(), &result); err != nil {
				t.Fatal(err, out.String())
			}
			if result.Realms == nil || len(result.Realms) != len(realms) || stderr.Len() != 0 {
				t.Fatal(out.String(), stderr.String())
			}
			if !empty && (!result.Realms[0].Selected || result.Realms[0].Type != "PvP" || result.Realms[0].Status != "online" || result.Realms[0].Characters != 2) {
				t.Fatal(out.String())
			}
		})
	}
}

func TestRealmCommands(t *testing.T) {
	realms := []auth.Realm{
		{ID: 7, Name: "First Realm", Address: "127.0.0.1:8085", Characters: 2, Population: 0.5},
		{ID: 8, Name: "Second Realm", Address: "127.0.0.1:8086", Type: 1},
		{ID: 9, Name: "Offline Realm", Address: "127.0.0.1:8087", Flags: auth.RealmFlagOffline},
		{ID: 10, Name: "Locked Realm", Address: "127.0.0.1:8088", Locked: true},
		{ID: 11, Name: "Old Realm", Address: "127.0.0.1:8089", Flags: auth.RealmFlagSpecifyBuild, Build: 8606},
	}
	for _, tc := range []struct {
		name       string
		args       []string
		selected   uint8
		wantID     uint8
		wantOutput []string
		wantErr    string
	}{
		{name: "list", args: []string{"list"}, selected: 7, wantID: 7, wantOutput: []string{"SELECTED", "*", "First Realm", "Second Realm", "offline", "locked", "incompatible build", "CHARACTERS"}},
		{name: "set ID", args: []string{"set", "8"}, selected: 7, wantID: 8, wantOutput: []string{"Selected realm: Second Realm (ID: 8)", "127.0.0.1:8086", "realm.json"}},
		{name: "set case insensitive name", args: []string{"set", "second realm"}, selected: 7, wantID: 8, wantOutput: []string{"Selected realm: Second Realm"}},
		{name: "status", args: []string{"status"}, selected: 7, wantID: 7, wantOutput: []string{"Selected realm: First Realm (ID: 7)", "Status: online", "Characters: 2", "Population: 0.50"}},
		{name: "offline status", args: []string{"status"}, selected: 9, wantID: 9, wantOutput: []string{"Status: offline"}},
		{name: "unknown realm", args: []string{"set", "unknown"}, selected: 7, wantID: 7, wantErr: "not found"},
		{name: "offline realm", args: []string{"set", "9"}, selected: 7, wantID: 7, wantErr: "offline; selection unchanged"},
		{name: "locked realm", args: []string{"set", "10"}, selected: 7, wantID: 7, wantErr: "locked; selection unchanged"},
		{name: "incompatible realm", args: []string{"set", "11"}, selected: 7, wantID: 7, wantErr: "incompatible build; selection unchanged"},
		{name: "removed realm status", args: []string{"status"}, selected: 42, wantID: 42, wantErr: "no longer listed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path, session := serveCommandRealms(t, realms, false)
			selected := auth.Realm{ID: tc.selected, Name: "Saved", Address: "localhost:8085"}
			if err := saveRealm(selected); err != nil {
				t.Fatal(err)
			}
			before, err := os.ReadFile(config.Get().RealmFilePath)
			if err != nil {
				t.Fatal(err)
			}
			cmd := newRealmCommand()
			cmd.SetArgs(append(tc.args, "--timeout", "1s"))
			var out bytes.Buffer
			cmd.SetOut(&out)
			cmd.SetErr(io.Discard)
			err = cmd.Execute()
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) || out.Len() != 0 {
					t.Fatalf("got %q, %v; want error %q", out.String(), err, tc.wantErr)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			for _, want := range tc.wantOutput {
				if !strings.Contains(out.String(), want) {
					t.Errorf("missing %q in %q", want, out.String())
				}
			}
			if tc.wantErr != "" || tc.args[0] != "set" {
				after, err := os.ReadFile(config.Get().RealmFilePath)
				if err != nil || !bytes.Equal(after, before) {
					t.Fatal("read or failed selection changed realm config")
				}
			}
			if err := config.Init(path); err != nil {
				t.Fatal(err)
			}
			if got, err := realmstore.Read(config.Get().RealmFilePath); err != nil || got.ID != tc.wantID {
				t.Fatalf("wrong persisted selection: %#v", got)
			}
			client, err := database.NewClient(config.Get().DataDir)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			got, err := client.GetSession()
			if err != nil || *got != *session {
				t.Fatal("realm command changed session")
			}
		})
	}
}

func TestRealmCommandValidation(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"list"}, "run 'agent-wow auth login'"},
		{[]string{"set", "1"}, "run 'agent-wow auth login'"},
		{[]string{"status"}, "no realm selected"},
		{[]string{"set"}, "accepts 1 arg(s)"},
		{[]string{"set", "one", "two"}, "accepts 1 arg(s)"},
		{[]string{"set", " "}, "must not be empty"},
		{[]string{"list", "--timeout", "0"}, "--timeout must be greater"},
		{[]string{"set", "1", "--timeout", "-1s"}, "--timeout must be greater"},
		{[]string{"status", "--timeout", "0"}, "--timeout must be greater"},
	} {
		t.Run(strings.Join(tc.args, " "), func(t *testing.T) {
			initTestConfig(t)
			cmd := newRealmCommand()
			cmd.SetArgs(tc.args)
			cmd.SetOut(io.Discard)
			cmd.SetErr(io.Discard)
			if err := cmd.Execute(); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("want %q, got %v", tc.want, err)
			}
		})
	}
}

func TestRealmStaleSession(t *testing.T) {
	serveCommandRealms(t, nil, true)
	cmd := newRealmCommand()
	cmd.SetArgs([]string{"set", "1"})
	cmd.SetOut(io.Discard)
	cmd.SetErr(io.Discard)
	if err := cmd.Execute(); err == nil || !strings.Contains(err.Error(), "auth login") {
		t.Fatalf("expected stale session guidance, got %v", err)
	}
	if _, err := realmstore.Read(config.Get().RealmFilePath); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("stale session saved a selection")
	}
}

func TestSelectLoginRealm(t *testing.T) {
	realms := []auth.Realm{
		{ID: 1, Name: "Offline", Address: "localhost:8085", Flags: auth.RealmFlagOffline},
		{ID: 2, Name: "Locked", Address: "localhost:8086", Locked: true},
		{ID: 3, Name: "Available", Address: "localhost:8087"},
		{ID: 4, Name: "Preferred", Address: "localhost:8088"},
	}
	for _, tc := range []struct {
		name     string
		selected uint8
		list     []auth.Realm
		wantID   uint8
		wantErr  string
	}{
		{"first login", 0, realms, 3, ""},
		{"preserve choice and refresh address", 4, realms, 4, ""},
		{"unavailable choice", 1, realms, 1, "offline"},
		{"missing choice", 5, realms, 5, "no longer listed"},
		{"empty list", 0, nil, 0, "no available realms"},
		{"no selectable realms", 0, realms[:2], 0, "no available realms"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := initTestConfig(t)
			if tc.selected != 0 {
				if err := saveRealm(auth.Realm{ID: tc.selected, Name: "Saved", Address: "old.example:8085"}); err != nil {
					t.Fatal(err)
				}
			}
			err := selectLoginRealm(tc.list)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("want %q, got %v", tc.wantErr, err)
				}
			} else {
				if err != nil {
					t.Fatal(err)
				}
			}
			if err := config.Init(path); err != nil {
				t.Fatal(err)
			}
			got, err := realmstore.Read(config.Get().RealmFilePath)
			if tc.wantID == 0 {
				if !errors.Is(err, os.ErrNotExist) {
					t.Fatal("saved an unavailable realm")
				}
			} else if err != nil || got.ID != tc.wantID {
				t.Fatalf("incorrect persisted selection: %#v", got)
			}
			if tc.selected == 4 && got.Address != "localhost:8088" {
				t.Fatal("login did not refresh selected realm address")
			}
		})
	}
}

func TestFindRealmAmbiguous(t *testing.T) {
	realms := []auth.Realm{{ID: 1, Name: "Duplicate"}, {ID: 2, Name: "DUPLICATE"}}
	if _, err := findRealm(realms, "duplicate"); err == nil || !strings.Contains(err.Error(), "ambiguous") {
		t.Fatalf("accepted ambiguous name: %v", err)
	}
	if got, err := findRealm(realms, "2"); err != nil || got.ID != 2 {
		t.Fatalf("failed ID disambiguation: %v", err)
	}
}

func TestLoadSelectedRealm(t *testing.T) {
	configPath := initTestConfig(t)
	path := filepath.Join(t.TempDir(), "custom-realm.json")
	t.Setenv("AGENT_WOW_REALM_FILE_PATH", path)
	if err := config.Init(configPath); err != nil {
		t.Fatal(err)
	}
	if got, err := loadSelectedRealm(); got != nil || err != nil {
		t.Fatalf("missing selection: got %v, %v", got, err)
	}
	for _, want := range []realmstore.Realm{
		{ID: 1, Name: "First", Address: "localhost:8085"},
		{ID: 2, Name: "Second", Address: "localhost:8086"},
	} {
		if err := realmstore.Write(path, want); err != nil {
			t.Fatal(err)
		}
		got, err := loadSelectedRealm()
		if err != nil || got == nil || *got != want {
			t.Fatalf("did not read current selection from the configured path: %v", err)
		}
	}
	if err := os.WriteFile(path, []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	// Loading application settings must not read realm files as a side effect.
	if err := config.Init(configPath); err != nil {
		t.Fatal(err)
	}
	if got, err := loadSelectedRealm(); got != nil || err == nil {
		t.Fatal("malformed realm file was treated as an absent selection")
	}
	if err := selectLoginRealm([]auth.Realm{{ID: 3, Name: "Available", Address: "localhost:8087"}}); err == nil {
		t.Fatal("login ignored malformed realm file")
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "{" {
		t.Fatal("login replaced malformed realm file")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if got, err := loadSelectedRealm(); got != nil || err == nil || errors.Is(err, os.ErrNotExist) {
		t.Fatal("realm file read error was treated as a missing selection")
	}
}

func serveCommandRealms(t *testing.T, realms []auth.Realm, stale bool) (string, *auth.Session) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	host, port, _ := net.SplitHostPort(listener.Addr().String())
	t.Setenv("AGENT_WOW_AUTHSERVER_HOST", host)
	t.Setenv("AGENT_WOW_AUTHSERVER_PORT", port)
	path := initTestConfig(t)
	session := &auth.Session{Username: "PLAYER", Key: [40]byte{1, 2, 3}}
	saveTestSession(t, session)
	done := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(3 * time.Second))
		done <- func() error {
			var request [40]byte
			if _, err := io.ReadFull(conn, request[:]); err != nil {
				return err
			}
			if request[0] != 2 || string(request[34:]) != "PLAYER" {
				return errors.New("realm command did not reconnect")
			}
			challenge := make([]byte, 34)
			challenge[0], challenge[2] = 2, 99
			if _, err := conn.Write(challenge); err != nil {
				return err
			}
			var proof [58]byte
			if _, err := io.ReadFull(conn, proof[:]); err != nil {
				return err
			}
			hash := sha1.New()
			hash.Write([]byte(session.Username))
			hash.Write(proof[1:17])
			hash.Write(challenge[2:18])
			hash.Write(session.Key[:])
			if !bytes.Equal(proof[17:37], hash.Sum(nil)) {
				return errors.New("realm command did not use saved key")
			}
			if stale {
				return nil
			}
			if _, err := conn.Write([]byte{3, 0, 0, 0}); err != nil {
				return err
			}
			var listRequest [5]byte
			if _, err := io.ReadFull(conn, listRequest[:]); err != nil {
				return err
			}
			if listRequest != [5]byte{0x10} {
				return errors.New("incorrect realm list request")
			}
			body := binary.LittleEndian.AppendUint16(make([]byte, 4), uint16(len(realms)))
			for _, realm := range realms {
				locked := byte(0)
				if realm.Locked {
					locked = 1
				}
				body = append(body, realm.Type, locked, realm.Flags)
				body = append(body, []byte(realm.Name+"\x00"+realm.Address+"\x00")...)
				body = binary.LittleEndian.AppendUint32(body, math.Float32bits(realm.Population))
				body = append(body, realm.Characters, realm.Timezone, realm.ID)
				if realm.Flags&auth.RealmFlagSpecifyBuild != 0 {
					body = append(body, realm.Version[:]...)
					body = binary.LittleEndian.AppendUint16(body, realm.Build)
				}
			}
			body = append(body, 0x10, 0)
			packet := append(binary.LittleEndian.AppendUint16([]byte{0x10}, uint16(len(body))), body...)
			_, err := conn.Write(packet)
			return err
		}()
	}()
	t.Cleanup(func() {
		listener.Close()
		if err := <-done; err != nil {
			t.Errorf("test authserver: %v", err)
		}
	})
	return path, session
}
