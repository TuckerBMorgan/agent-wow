package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
)

type fakeCharacterClient struct {
	characters []char.Character
	create     func(context.Context, char.CreateOptions) (char.CreateResult, error)
	delete     func(context.Context, char.GUID) error
	closed     bool
	listErr    error
}

func (c *fakeCharacterClient) Close() error { c.closed = true; return nil }
func (c *fakeCharacterClient) List(ctx context.Context) ([]char.Character, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return c.characters, c.listErr
}
func (c *fakeCharacterClient) Create(ctx context.Context, o char.CreateOptions) (char.CreateResult, error) {
	return c.create(ctx, o)
}
func (c *fakeCharacterClient) Delete(ctx context.Context, guid char.GUID) error {
	return c.delete(ctx, guid)
}

func setupCharacterRealm(t *testing.T) (auth.Realm, *auth.Session) {
	t.Helper()
	realm := auth.Realm{ID: 7, Name: "Live Realm", Address: "127.0.0.1:8085", Type: 1}
	_, session := serveCommandRealms(t, []auth.Realm{realm}, false)
	if err := saveRealm(auth.Realm{ID: 7, Name: "Old Name", Address: "old.invalid:8085"}); err != nil {
		t.Fatal(err)
	}
	return realm, session
}

func TestCharacterListCommands(t *testing.T) {
	for _, empty := range []bool{false, true} {
		for _, asJSON := range []bool{false, true} {
			t.Run(strings.Join([]string{map[bool]string{false: "characters", true: "empty"}[empty], map[bool]string{false: "table", true: "json"}[asJSON]}, "/"), func(t *testing.T) {
				realm, session := setupCharacterRealm(t)
				client := &fakeCharacterClient{}
				if !empty {
					client.characters = []char.Character{{GUID: 9007199254740993, Name: "Arlen", Race: char.RaceHuman, Class: char.ClassWarrior, Gender: char.GenderMale, Level: 60, ZoneID: 12}}
				}
				deps := characterCommands{dial: func(ctx context.Context, got auth.Realm, s *auth.Session) (characterClient, error) {
					if got != realm || *s != *session {
						t.Fatal("did not refresh realm or use saved session")
					}
					return client, nil
				}}
				command := deps.command()
				args := []string{"list"}
				if asJSON {
					args = append(args, "--json")
				}
				command.SetArgs(args)
				var out, stderr bytes.Buffer
				command.SetOut(&out)
				command.SetErr(&stderr)
				if err := command.Execute(); err != nil {
					t.Fatal(err)
				}
				if !client.closed {
					t.Fatal("client not closed")
				}
				if asJSON {
					var result struct {
						Realm struct {
							ID   uint8  `json:"id"`
							Name string `json:"name"`
						} `json:"realm"`
						Characters []struct {
							GUID     string `json:"guid"`
							Race     string `json:"race"`
							ZoneID   uint32 `json:"zone_id"`
							ZoneName string `json:"zone_name"`
							MapID    uint32 `json:"map_id"`
							MapName  string `json:"map_name"`
						} `json:"characters"`
					}
					if err := json.Unmarshal(out.Bytes(), &result); err != nil {
						t.Fatal(err, out.String())
					}
					if result.Realm.ID != 7 || result.Realm.Name != "Live Realm" || result.Characters == nil {
						t.Fatal(out.String())
					}
					if !empty && (len(result.Characters) != 1 || result.Characters[0].GUID != "9007199254740993" || result.Characters[0].Race != "Human") {
						t.Fatal(out.String())
					}
					if !empty {
						character := result.Characters[0]
						if character.ZoneID != 12 || character.ZoneName != "Elwynn Forest" || character.MapID != 0 || character.MapName != "Eastern Kingdoms" {
							t.Fatal("missing location labels or IDs", out.String())
						}
					}
				} else if !strings.Contains(out.String(), "Realm: Live Realm") || empty && !strings.Contains(out.String(), "No characters") || !empty && !strings.Contains(out.String(), "Arlen") {
					t.Fatal(out.String())
				}
				if !asJSON && !empty {
					if !strings.Contains(out.String(), "Elwynn Forest") || !strings.Contains(out.String(), "Eastern Kingdoms") || strings.Contains(out.String(), "ZONE ID") || strings.Contains(out.String(), "MAP ID") {
						t.Fatal("missing location labels in table", out.String())
					}
				}
				if stderr.Len() != 0 {
					t.Fatal("unexpected stderr", stderr.String())
				}
			})
		}
	}
}

func TestCharacterCreateOptions(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		t.Run(map[bool]string{false: "randomized", true: "explicit zeros"}[explicit], func(t *testing.T) {
			setupCharacterRealm(t)
			client := &fakeCharacterClient{create: func(ctx context.Context, o char.CreateOptions) (char.CreateResult, error) {
				if explicit {
					if o.Name == nil || *o.Name != "Arlen" || o.Race == nil || *o.Race != char.RaceHuman || o.Class == nil || *o.Class != char.ClassWarrior || o.Gender == nil || *o.Gender != char.GenderMale {
						t.Fatal("missing explicit identity")
					}
					for _, p := range []*uint8{o.Skin, o.Face, o.HairStyle, o.HairColor, o.FacialHair} {
						if p == nil || *p != 0 {
							t.Fatal("lost explicit zero")
						}
					}
				} else if o != (char.CreateOptions{}) {
					t.Fatal("omitted options became defaults", o)
				}
				return char.CreateResult{Name: "Arlen", Race: char.RaceHuman, Class: char.ClassWarrior, Gender: char.GenderMale}, nil
			}}
			command := characterCommands{dial: func(context.Context, auth.Realm, *auth.Session) (characterClient, error) { return client, nil }}.command()
			args := []string{"create"}
			if explicit {
				args = append(args, "--name", "Arlen", "--race", "human", "--class", "1", "--gender", "0", "--skin", "0", "--face", "0", "--hair-style", "0", "--hair-color", "0", "--facial-hair", "0")
			}
			command.SetArgs(args)
			var out bytes.Buffer
			command.SetOut(&out)
			command.SetErr(io.Discard)
			if err := command.Execute(); err != nil {
				t.Fatal(err)
			}
			for _, want := range []string{"Created character: Arlen", "Realm: Live Realm", "Race: Human", "Class: Warrior", "Gender: Male", "Hair color: 0"} {
				if !strings.Contains(out.String(), want) {
					t.Fatal(out.String())
				}
			}
			if !client.closed {
				t.Fatal("not closed")
			}
		})
	}
}

func TestCharacterDeleteConfirmation(t *testing.T) {
	for _, tc := range []struct {
		name, answer string
		yes, changed bool
		wantCalls    int
		wantErr      string
	}{
		{"yes flag", "", true, false, 1, ""}, {"confirm", "yes\n", false, false, 1, ""}, {"decline", "n\n", false, false, 0, ""},
		{"EOF", "", false, false, 0, ""}, {"changed target", "y\n", false, true, 0, "changed or disappeared"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			realm, session := setupCharacterRealm(t)
			owned := char.Character{GUID: 99, Name: "Arlen", Race: char.RaceHuman, Class: char.ClassWarrior}
			var clients []*fakeCharacterClient
			deletions := 0
			var stderr bytes.Buffer
			deps := characterCommands{isTerminal: func(io.Reader) bool { return true }, dial: func(ctx context.Context, r auth.Realm, s *auth.Session) (characterClient, error) {
				if r != realm || *s != *session {
					t.Fatal("reconnected to wrong realm or account")
				}
				if len(clients) > 0 && !clients[0].closed {
					t.Fatal("connection left open during prompt")
				}
				if err := ctx.Err(); err != nil {
					t.Fatal("reused preflight context", err)
				}
				client := &fakeCharacterClient{characters: []char.Character{owned}, delete: func(ctx context.Context, guid char.GUID) error {
					if !tc.yes && (len(clients) != 2 || !strings.Contains(stderr.String(), "Delete Arlen (GUID: 99) on Live Realm")) {
						t.Fatal("mutation before confirmation and recheck")
					}
					if guid != 99 {
						t.Fatal("wrong target", guid)
					}
					deletions++
					return nil
				}}
				if tc.changed && len(clients) > 0 {
					client.characters[0].Name = "Someoneelse"
				}
				clients = append(clients, client)
				return client, nil
			}}
			command := deps.command()
			args := []string{"delete", "arlen"}
			if tc.yes {
				args = append(args, "--yes")
			}
			command.SetArgs(args)
			var out bytes.Buffer
			command.SetOut(&out)
			command.SetErr(&stderr)
			command.SetIn(strings.NewReader(tc.answer))
			err := command.Execute()
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatal(err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			if deletions != tc.wantCalls {
				t.Fatal("wrong deletion count", deletions)
			}
			for _, client := range clients {
				if !client.closed {
					t.Fatal("leaked connection")
				}
			}
			if deletions == 0 && strings.Contains(out.String(), "Deleted character") {
				t.Fatal("reported unconfirmed deletion")
			}
		})
	}
}

func TestCharacterValidation(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"create", "Arlen"}, "unknown command"}, {[]string{"create", "--name", ""}, "must not be empty"},
		{[]string{"create", "--race", "goblin"}, "unknown race"}, {[]string{"create", "--class", "10"}, "unknown class"},
		{[]string{"create", "--gender", "2"}, "unknown gender"}, {[]string{"create", "--skin", "256"}, "invalid argument"},
		{[]string{"create", "--face", "-1"}, "invalid argument"}, {[]string{"list", "--timeout", "0"}, "greater than zero"},
		{[]string{"list"}, "no realm selected"}, {[]string{"delete", "Arlen"}, "requires --yes"},
		{[]string{"delete", "", "--yes"}, "must not be empty"},
	} {
		t.Run(strings.Join(tc.args, " "), func(t *testing.T) {
			initTestConfig(t)
			command := newCharCommand()
			command.SetArgs(tc.args)
			command.SetIn(strings.NewReader("yes\n"))
			command.SetOut(io.Discard)
			command.SetErr(io.Discard)
			if err := command.Execute(); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("want %q, got %v", tc.want, err)
			}
		})
	}
}

func TestFindCharacter(t *testing.T) {
	chars := []char.Character{{GUID: 99, Name: "Arlen"}, {GUID: 100, Name: "Mira"}}
	for _, selector := range []string{"Arlen", "ARLEN", "99", "0x63", "099"} {
		got, err := findCharacter(chars, selector)
		if err != nil || got.GUID != 99 {
			t.Fatal(selector, got, err)
		}
	}
	if _, err := findCharacter(chars, "0"); err == nil {
		t.Fatal("accepted unknown GUID")
	}
	chars = append(chars, char.Character{GUID: 101, Name: "ARLEN"})
	if _, err := findCharacter(chars, "arlen"); err == nil || !strings.Contains(err.Error(), "ambiguous") {
		t.Fatal(err)
	}
}

func TestCharacterFailuresHaveNoSuccessOutput(t *testing.T) {
	setupCharacterRealm(t)
	client := &fakeCharacterClient{create: func(context.Context, char.CreateOptions) (char.CreateResult, error) {
		return char.CreateResult{}, &char.OutcomeUnknownError{Operation: "create", Name: "Arlen", Err: context.DeadlineExceeded}
	}}
	command := characterCommands{dial: func(context.Context, auth.Realm, *auth.Session) (characterClient, error) { return client, nil }}.command()
	command.SetArgs([]string{"create"})
	var out bytes.Buffer
	command.SetOut(&out)
	command.SetErr(io.Discard)
	if err := command.Execute(); !errors.Is(err, context.DeadlineExceeded) || out.Len() != 0 || !client.closed {
		t.Fatal(err, out.String())
	}
}

type delayedConfirmation struct {
	io.Reader
	delay time.Duration
}

func (r *delayedConfirmation) Read(b []byte) (int, error) {
	if r.delay > 0 {
		time.Sleep(r.delay)
		r.delay = 0
	}
	return r.Reader.Read(b)
}

func TestDeletionPromptDoesNotConsumeTimeout(t *testing.T) {
	setupCharacterRealm(t)
	dials := 0
	deleted := false
	deps := characterCommands{isTerminal: func(io.Reader) bool { return true }, dial: func(ctx context.Context, _ auth.Realm, _ *auth.Session) (characterClient, error) {
		dials++
		if err := ctx.Err(); err != nil {
			t.Fatal(err)
		}
		return &fakeCharacterClient{characters: []char.Character{{GUID: 99, Name: "Arlen"}}, delete: func(ctx context.Context, _ char.GUID) error { deleted = true; return ctx.Err() }}, nil
	}}
	command := deps.command()
	command.SetArgs([]string{"delete", "99", "--timeout", "100ms"})
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	command.SetIn(&delayedConfirmation{Reader: strings.NewReader("yes\n"), delay: 150 * time.Millisecond})
	if err := command.Execute(); err != nil || dials != 2 || !deleted {
		t.Fatal(err, dials, deleted)
	}
}
