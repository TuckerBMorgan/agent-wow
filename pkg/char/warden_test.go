package char

import (
	"bytes"
	"context"
	"crypto/rc4"
	"crypto/sha1"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"testing"
	"time"
)

// Exercise both layers of RC4 independently: the existing peer frames encrypted
// world headers and this peer encrypts Warden payloads across the owner handoff.
func TestWardenAcrossAuthenticationSelectionAndGameplay(t *testing.T) {
	decode := func(s string) []byte {
		b, err := hex.DecodeString(s)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	saved := testSession()
	maintained := make(chan struct{})
	realm := testRealm(t, func(p *realmPeer) error {
		send, _ := rc4.NewCipher(decode("96d79bec6044480918ee080579d8901a"))
		recv, _ := rc4.NewCipher(decode("e711c90e3921b9725e3ccc8673099955"))
		write := func(body []byte) error {
			data := append([]byte(nil), body...)
			send.XORKeyStream(data, data)
			return p.write(0x2e6, data)
		}
		read := func(want []byte) error {
			body, err := p.expect(0x2e7)
			if err != nil {
				return err
			}
			recv.XORKeyStream(body, body)
			if !bytes.Equal(body, want) {
				return fmt.Errorf("Warden reply %x, want %x", body, want)
			}
			return nil
		}
		p.beforeAuthOK = func() error {
			request := decode("0079c0768d657977d697e10bad956cced1ae25bc51063b77bd363c3efe0fc173f9")
			request = binary.LittleEndian.AppendUint32(request, 18756)
			if err := write(request); err != nil {
				return err
			}
			return read([]byte{1})
		}
		if err := p.authenticate(saved, false); err != nil {
			return err
		}
		if _, err := p.expect(0x037); err != nil {
			return err
		}
		if err := write(decode("054d808d2c77d905c41a6380ec08586afe")); err != nil {
			return err
		}
		if err := read(decode("04568c054c781a972a6037a2290c22b52571a06f4e")); err != nil {
			return err
		}
		send, _ = rc4.NewCipher(decode("c2b7adedfccca9c2bfb3f85602ba809b"))
		recv, _ = rc4.NewCipher(decode("7f96eefda5b63d20a4df8e00cbf48304"))
		if err := p.write(0x03b, characterFixture(t)); err != nil {
			return err
		}
		if _, err := p.expect(0x03d); err != nil {
			return err
		}
		checksum := func(data []byte) uint32 {
			digest := sha1.Sum(data)
			var n uint32
			for i := 0; i < 20; i += 4 {
				n ^= binary.LittleEndian.Uint32(digest[i:])
			}
			return n
		}
		var init []byte
		for _, data := range []string{"01000100804f0200c01802003025020010290200", "0400001092410001", "01010020ae460001"} {
			block := decode(data)
			init = append(init, 3)
			init = binary.LittleEndian.AppendUint16(init, uint16(len(block)))
			init = binary.LittleEndian.AppendUint32(init, checksum(block))
			init = append(init, block...)
		}
		if err := write(init); err != nil {
			return err
		}
		if err := p.write(0x236, make([]byte, 20)); err != nil {
			return err
		}
		for i := uint32(0); i < 3; i++ {
			// Interleave recurring Warden checks and world time synchronization.
			if err := write(decode("0200288c0033849800057f")); err != nil {
				return err
			}
			response, err := p.expect(0x2e7)
			if err != nil {
				return err
			}
			recv.XORKeyStream(response, response)
			if len(response) != 18 || response[0] != 2 || binary.LittleEndian.Uint16(response[1:]) != 11 || binary.LittleEndian.Uint32(response[3:]) != checksum(response[7:]) || !bytes.Equal(response[12:], decode("0075440fb75e")) {
				return fmt.Errorf("bad periodic Warden reply %x", response)
			}
			if err := p.write(0x390, binary.LittleEndian.AppendUint32(nil, i)); err != nil {
				return err
			}
			body, err := p.expect(0x391)
			if err != nil || len(body) != 8 || binary.LittleEndian.Uint32(body) != i {
				return fmt.Errorf("time sync %x: %v", body, err)
			}
		}
		close(maintained)
		if _, err := p.expect(0x04b); err != nil {
			return err
		}
		return p.write(0x04d, nil)
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	client, err := Dial(ctx, realm, saved)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	session, err := client.EnterWorld(ctx, 99, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	// Closing the old owner cannot discard the Warden or world cipher state.
	client.Close()
	// The peer still has two sync rounds to finish after EnterWorld reports ready.
	select {
	case <-maintained:
	case <-ctx.Done():
		t.Fatal("maintenance did not finish", ctx.Err())
	}
	if err := session.Logout(ctx); err != nil {
		t.Fatal(err)
	}
}
