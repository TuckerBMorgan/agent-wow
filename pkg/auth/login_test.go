package auth

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"
)

func TestLogin(t *testing.T) {
	for _, tc := range []struct {
		name     string
		password string
		mutate   func([]byte) []byte
		wantErr  string
	}{
		{name: "success", password: "password"},
		{name: "wrong password", password: "wrong", wantErr: "incorrect account name or password"},
		{name: "forged server proof", password: "password", mutate: func(p []byte) []byte { p[2] ^= 1; return p }, wantErr: "proof verification failed"},
		{name: "truncated proof", password: "password", mutate: func(p []byte) []byte { return p[:10] }, wantErr: "unexpected EOF"},
		{name: "wrong opcode", password: "password", mutate: func(p []byte) []byte { p[0] = 2; return p }, wantErr: "unexpected logon proof opcode"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := testChallenge(t)
			address := serve(t, func(conn net.Conn) error {
				var request [40]byte
				if _, err := io.ReadFull(conn, request[:]); err != nil {
					return err
				}
				// Validate a complete 3.3.5a challenge independently of its encoder.
				if request[0] != 0 || request[1] != 8 || binary.LittleEndian.Uint16(request[2:4]) != 36 ||
					string(request[4:8]) != "WoW\x00" || !bytes.Equal(request[8:11], []byte{3, 3, 5}) ||
					binary.LittleEndian.Uint16(request[11:13]) != 12340 || string(request[13:17]) != "68x\x00" ||
					string(request[17:21]) != "niW\x00" || string(request[21:25]) != "SUne" ||
					request[33] != 6 || string(request[34:]) != "PLAYER" {
					return fmt.Errorf("invalid logon challenge: %x", request)
				}
				// Split the reply into single-byte writes to exercise stream framing.
				for _, b := range challengePacket(c) {
					if _, err := conn.Write([]byte{b}); err != nil {
						return err
					}
				}
				var requestProof [75]byte
				if _, err := io.ReadFull(conn, requestProof[:]); err != nil {
					return err
				}
				if requestProof[0] != 1 || !bytes.Equal(requestProof[53:], make([]byte, 22)) {
					return errors.New("invalid logon proof packet")
				}
				// Verify the client using the server equation, never calculateProof.
				identity := digest([]byte("PLAYER:PASSWORD"))
				x := digest(c.salt[:], identity[:])
				v := new(big.Int).Exp(big.NewInt(7), fromLittleEndian(x[:]), modulus)
				u := digest(requestProof[1:33], c.public[:])
				base := new(big.Int).Mul(fromLittleEndian(requestProof[1:33]), new(big.Int).Exp(v, fromLittleEndian(u[:]), modulus))
				var private [32]byte
				for i := range private {
					private[i] = byte(64 + i)
				}
				key := sessionKey(toLittleEndian(new(big.Int).Exp(base, fromLittleEndian(private[:]), modulus)))
				n := toLittleEndian(modulus)
				group, gHash := digest(n[:]), digest([]byte{7})
				for i := range group {
					group[i] ^= gHash[i]
				}
				name := digest([]byte("PLAYER"))
				m1 := digest(group[:], name[:], c.salt[:], requestProof[1:33], c.public[:], key[:])
				if !bytes.Equal(requestProof[33:53], m1[:]) {
					_, err := conn.Write([]byte{1, 4})
					return err
				}
				m2 := digest(requestProof[1:33], m1[:], key[:])
				response := make([]byte, 32)
				response[0] = 1
				copy(response[2:22], m2[:])
				binary.LittleEndian.PutUint32(response[22:26], 0x00800000)
				if tc.mutate != nil {
					response = tc.mutate(response)
				}
				_, err := conn.Write(response)
				return err
			})
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			client, err := NewClient(address)
			if err != nil {
				t.Fatal(err)
			}
			session, err := client.Login(ctx, "player", tc.password)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) || session != nil {
					t.Fatalf("expected %q and no session; got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if session.Username != "PLAYER" || session.AccountFlags != 0x00800000 || session.Key == [40]byte{} {
				t.Fatal("incorrect session fields")
			}
		})
	}
}

func TestChallengeValidation(t *testing.T) {
	valid := challengePacket(testChallenge(t))
	for _, tc := range []struct {
		name   string
		offset int
		value  byte
		want   string
	}{
		{"opcode", 0, 1, "unexpected opcode"},
		{"banned", 2, 3, "banned"},
		{"unknown account", 2, 4, "incorrect account"},
		{"unknown error", 2, 0xfe, "code 0xfe"},
		{"generator length", 35, 255, "generator length"},
		{"generator", 36, 3, "unsupported SRP6 group"},
		{"modulus length", 37, 255, "modulus length"},
		{"modulus", 38, 0, "unsupported SRP6 group"},
		{"authenticator", 118, 4, "unsupported account security"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			packet := bytes.Clone(valid)
			packet[tc.offset] = tc.value
			if _, err := readChallenge(bytes.NewReader(packet)); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("expected %q, got %v", tc.want, err)
			}
		})
	}
	for length := 0; length < len(valid); length++ {
		if _, err := readChallenge(bytes.NewReader(valid[:length])); err == nil {
			t.Fatalf("accepted challenge truncated at %d", length)
		}
	}
}

func TestLoginCancellation(t *testing.T) {
	for _, deadline := range []bool{false, true} {
		t.Run(fmt.Sprintf("deadline=%v", deadline), func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			if deadline {
				cancel()
				ctx, cancel = context.WithTimeout(context.Background(), 50*time.Millisecond)
			}
			defer cancel()
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
			session, err := client.Login(ctx, "player", "password")
			var timeout net.Error
			if session != nil || err == nil || !(errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) || errors.As(err, &timeout) && timeout.Timeout()) {
				t.Fatalf("expected cancellation/timeout, got %v", err)
			}
		})
	}
}

func challengePacket(c challenge) []byte {
	packet := []byte{0, 0, 0}
	packet = append(packet, c.public[:]...)
	packet = append(packet, 1, 7, 32)
	n := toLittleEndian(modulus)
	packet = append(packet, n[:]...)
	packet = append(packet, c.salt[:]...)
	return append(packet, make([]byte, 17)...)
}
