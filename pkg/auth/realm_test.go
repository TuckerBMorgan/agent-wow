package auth

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"reflect"
	"strings"
	"testing"
	"time"
)

func realmListPacket() []byte {
	// Two WotLK entries, including optional build data and an IPv6 endpoint.
	body := []byte("\x00\x00\x00\x00\x02\x00" +
		"\x00\x00\x04AzerothCore\x00127.0.0.1:8085\x00\x00\x00\x00\x3f\x02\x01\x07\x03\x03\x05\x34\x30" +
		"\x01\x01\x02Offline Realm\x00[::1]:8086\x00\x00\x00\x80\x3f\x00\x01\x08" +
		"\x10\x00")
	return append(binary.LittleEndian.AppendUint16([]byte{0x10}, uint16(len(body))), body...)
}

func TestReadRealms(t *testing.T) {
	got, err := readRealms(bytes.NewReader(realmListPacket()))
	if err != nil {
		t.Fatal(err)
	}
	want := []Realm{
		{ID: 7, Name: "AzerothCore", Address: "127.0.0.1:8085", Flags: RealmFlagSpecifyBuild, Population: 0.5, Characters: 2, Timezone: 1, Version: [3]byte{3, 3, 5}, Build: 12340},
		{ID: 8, Name: "Offline Realm", Address: "[::1]:8086", Type: 1, Locked: true, Flags: RealmFlagOffline, Population: 1, Timezone: 1},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("realms = %#v; want %#v", got, want)
	}
	got, err = readRealms(bytes.NewReader([]byte{0x10, 8, 0, 0, 0, 0, 0, 0, 0, 0x10, 0}))
	if err != nil || len(got) != 0 {
		t.Fatalf("empty realm list: %v, %v", got, err)
	}
}

func TestReadRealmsMalformed(t *testing.T) {
	valid := realmListPacket()
	for length := 0; length < len(valid); length++ {
		if realms, err := readRealms(bytes.NewReader(valid[:length])); err == nil || realms != nil {
			t.Fatalf("accepted truncated packet of length %d", length)
		}
	}
	for _, tc := range []struct {
		name   string
		mutate func([]byte) []byte
	}{
		{"opcode", func(p []byte) []byte { p[0] = 0; return p }},
		{"short body", func(p []byte) []byte { p[1], p[2] = 7, 0; return p }},
		{"impossible count", func(p []byte) []byte { p[7], p[8] = 255, 255; return p }},
		{"trailing records", func(p []byte) []byte { p[7] = 1; return p }},
		{"trailer", func(p []byte) []byte { p[len(p)-2] = 0; return p }},
		{"duplicate ID", func(p []byte) []byte { p[len(p)-3] = 7; return p }},
		{"empty name", func(p []byte) []byte { p[12] = 0; return p }},
		{"name with terminal escape", func(p []byte) []byte { p[12] = 0x1b; return p }},
		{"invalid UTF-8", func(p []byte) []byte { p[12] = 0xff; return p }},
		{"bad address", func(p []byte) []byte { return bytes.Replace(p, []byte(":8085"), []byte(":0000"), 1) }},
		{"infinite population", func(p []byte) []byte {
			i := bytes.Index(p, []byte{0, 0, 0, 0x3f})
			p[i+2], p[i+3] = 0x80, 0x7f
			return p
		}},
		{"missing build", func(p []byte) []byte {
			p[len(p)-2] = 0
			p = p[:len(p)-4]
			binary.LittleEndian.PutUint16(p[1:3], uint16(len(p)-3))
			return p
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if realms, err := readRealms(bytes.NewReader(tc.mutate(bytes.Clone(valid)))); err == nil || realms != nil {
				t.Fatalf("accepted malformed realm list: %v", realms)
			}
		})
	}
}

func TestRealmAvailability(t *testing.T) {
	for _, tc := range []struct {
		realm  Realm
		status string
	}{
		{Realm{}, "online"},
		{Realm{Flags: RealmFlagSpecifyBuild, Build: 12340}, "online"},
		{Realm{Flags: RealmFlagOffline}, "offline"},
		{Realm{Flags: RealmFlagInvalid}, "invalid"},
		{Realm{Locked: true}, "locked"},
		{Realm{Flags: RealmFlagSpecifyBuild, Build: 8606}, "incompatible build"},
		{Realm{Flags: RealmFlagOffline, Locked: true}, "offline, locked"},
	} {
		if tc.realm.Status() != tc.status || tc.realm.Selectable() != (tc.status == "online") {
			t.Errorf("status for %#v = %q, want %q", tc.realm, tc.realm.Status(), tc.status)
		}
	}
}

func TestListRealmsUsesSavedSession(t *testing.T) {
	for _, stale := range []bool{false, true} {
		t.Run(map[bool]string{false: "valid", true: "stale"}[stale], func(t *testing.T) {
			session := &Session{Username: "PLAYER", Key: [40]byte{1, 2, 3}}
			address := serve(t, func(conn net.Conn) error {
				if err := acceptRealmReconnect(conn, session, stale); err != nil || stale {
					return err
				}
				var request [5]byte
				if _, err := io.ReadFull(conn, request[:]); err != nil {
					return err
				}
				if request != [5]byte{0x10} {
					return errors.New("incorrect realm list request")
				}
				for _, b := range realmListPacket() {
					if _, err := conn.Write([]byte{b}); err != nil {
						return err
					}
				}
				return nil
			})
			client, err := NewClient(address)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			realms, err := client.ListRealms(ctx, session)
			if stale {
				if err == nil || !strings.Contains(err.Error(), "session may be invalid") || realms != nil {
					t.Fatalf("stale session: got %v, %v", realms, err)
				}
			} else if err != nil || len(realms) != 2 || realms[0].ID != 7 {
				t.Fatalf("realm list: got %v, %v", realms, err)
			}
		})
	}
}

func TestListRealmsCancellation(t *testing.T) {
	for _, deadline := range []bool{false, true} {
		t.Run(map[bool]string{false: "cancel", true: "deadline"}[deadline], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			if deadline {
				cancel()
				ctx, cancel = context.WithTimeout(context.Background(), 100*time.Millisecond)
			}
			defer cancel()
			session := &Session{Username: "PLAYER", Key: [40]byte{1, 2, 3}}
			address := serve(t, func(conn net.Conn) error {
				if err := acceptRealmReconnect(conn, session, false); err != nil {
					return err
				}
				var request [5]byte
				if _, err := io.ReadFull(conn, request[:]); err != nil {
					return err
				}
				if !deadline {
					cancel()
				}
				_, err := io.Copy(io.Discard, conn)
				return err
			})
			client, err := NewClient(address)
			if err != nil {
				t.Fatal(err)
			}
			realms, err := client.ListRealms(ctx, session)
			var timeout net.Error
			if realms != nil || err == nil || !(errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) || errors.As(err, &timeout) && timeout.Timeout()) {
				t.Fatalf("expected cancellation/timeout, got %v", err)
			}
		})
	}
}

func acceptRealmReconnect(conn net.Conn, session *Session, stale bool) error {
	var request [40]byte
	if _, err := io.ReadFull(conn, request[:]); err != nil {
		return err
	}
	if request[0] != 2 || string(request[34:]) != session.Username {
		return errors.New("expected reconnect")
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
	want := digest([]byte(session.Username), proof[1:17], challenge[2:18], session.Key[:])
	if !bytes.Equal(proof[17:37], want[:]) {
		return errors.New("incorrect saved session key")
	}
	if stale {
		return nil
	}
	_, err := conn.Write([]byte{3, 0, 0, 0})
	return err
}
