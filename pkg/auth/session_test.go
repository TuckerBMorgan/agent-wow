package auth

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func TestCheckSession(t *testing.T) {
	for _, tc := range []struct {
		name      string
		challenge []byte
		response  []byte
		staleKey  bool
		wantErr   string
	}{
		{name: "valid", response: []byte{3, 0, 0, 0}},
		{name: "stale key", staleKey: true, wantErr: "session may be invalid"},
		{name: "unknown account", challenge: []byte{2, 4}, wantErr: "incorrect account"},
		{name: "wrong challenge opcode", challenge: []byte{0, 0}, wantErr: "unexpected reconnect opcode"},
		{name: "truncated challenge", challenge: []byte{2, 0, 1}, wantErr: "unexpected EOF"},
		{name: "proof rejected", response: []byte{3, 9}, wantErr: "client version rejected"},
		{name: "wrong proof opcode", response: []byte{1, 0, 0, 0}, wantErr: "unexpected reconnect opcode"},
		{name: "truncated result", response: []byte{3}, wantErr: "unexpected EOF"},
		{name: "truncated flags", response: []byte{3, 0, 0}, wantErr: "unexpected EOF"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			key := [40]byte{1, 2, 3, 4}
			address := serve(t, func(conn net.Conn) error {
				var request [40]byte
				if _, err := io.ReadFull(conn, request[:]); err != nil {
					return err
				}
				if request[0] != 2 || request[1] != 8 || binary.LittleEndian.Uint16(request[2:4]) != 36 ||
					binary.LittleEndian.Uint16(request[11:13]) != 12340 || request[33] != 6 || string(request[34:]) != "PLAYER" {
					return errors.New("incorrect reconnect challenge")
				}
				if tc.challenge != nil {
					_, err := conn.Write(tc.challenge)
					return err
				}
				challenge := make([]byte, 34)
				challenge[0] = 2
				for i := 2; i < len(challenge); i++ {
					challenge[i] = byte(i)
				}
				for _, b := range challenge {
					if _, err := conn.Write([]byte{b}); err != nil {
						return err
					}
				}
				var proof [58]byte
				if _, err := io.ReadFull(conn, proof[:]); err != nil {
					return err
				}
				if proof[0] != 3 || proof[57] != 0 || bytes.Equal(proof[1:17], make([]byte, 16)) {
					return errors.New("incorrect reconnect proof framing or nonce")
				}
				hash := sha1.New()
				hash.Write([]byte("PLAYER"))
				hash.Write(proof[1:17])
				hash.Write(challenge[2:18])
				hash.Write(key[:])
				if !bytes.Equal(proof[17:37], hash.Sum(nil)) {
					if tc.staleKey {
						return nil // AzerothCore closes without a reply for a stale key.
					}
					return errors.New("incorrect reconnect digest")
				}
				if tc.staleKey {
					return errors.New("stale key unexpectedly matched")
				}
				hash.Reset()
				hash.Write(proof[1:17])
				hash.Write(make([]byte, 20))
				if !bytes.Equal(proof[37:57], hash.Sum(nil)) {
					return errors.New("incorrect reconnect version proof")
				}
				_, err := conn.Write(tc.response)
				return err
			})
			session := &Session{Username: "player", Key: key}
			if tc.staleKey {
				session.Key[0] ^= 1
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			client, err := NewClient(address)
			if err != nil {
				t.Fatal(err)
			}
			err = client.CheckSession(ctx, session)
			if tc.wantErr == "" && err != nil {
				t.Fatal(err)
			}
			if tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("expected %q, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestCheckSessionCancellation(t *testing.T) {
	for _, deadline := range []bool{false, true} {
		ctx, cancel := context.WithCancel(context.Background())
		if deadline {
			cancel()
			ctx, cancel = context.WithTimeout(context.Background(), 100*time.Millisecond)
		}
		address := serve(t, func(conn net.Conn) error {
			var request [40]byte
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
		err = client.CheckSession(ctx, &Session{Username: "PLAYER", Key: [40]byte{1}})
		cancel()
		var timeout net.Error
		if err == nil || !(errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) || errors.As(err, &timeout) && timeout.Timeout()) {
			t.Fatalf("expected cancellation/timeout, got %v", err)
		}
	}
}

func TestCheckSessionInvalid(t *testing.T) {
	client, err := NewClient("localhost:3724")
	if err != nil {
		t.Fatal(err)
	}
	for _, session := range []*Session{nil, {}, {Username: "PLAYER"}, {Username: "bad\nname", Key: [40]byte{1}}} {
		if err := client.CheckSession(context.Background(), session); err == nil || err.Error() != "invalid session" {
			t.Fatalf("expected invalid session error, got %v", err)
		}
	}
}
