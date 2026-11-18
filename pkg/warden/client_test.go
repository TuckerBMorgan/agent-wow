package warden

import (
	"bytes"
	"crypto/rc4"
	"encoding/binary"
	"strings"
	"testing"
)

type peer struct {
	client     *Client
	send, recv *rc4.Cipher
}

func newPeer() *peer {
	var key [40]byte
	for i := range key {
		key[i] = byte(i)
	}
	// Independent known-answer SessionKeyGenerator vector for key 00..27.
	send, _ := rc4.NewCipher(fromHex("96d79bec6044480918ee080579d8901a"))
	recv, _ := rc4.NewCipher(fromHex("e711c90e3921b9725e3ccc8673099955"))
	return &peer{New(key), send, recv}
}

func (p *peer) exchange(body []byte) ([]byte, error) {
	encrypted := make([]byte, len(body))
	p.send.XORKeyStream(encrypted, body)
	response, err := p.client.Handle(encrypted)
	if response != nil {
		p.recv.XORKeyStream(response, response)
	}
	return response, err
}

func moduleRequest() []byte {
	body := append([]byte{0}, moduleID...)
	body = append(body, moduleKey...)
	return binary.LittleEndian.AppendUint32(body, 18756)
}

func initRequest() []byte {
	var body []byte
	for _, block := range initializers {
		body = append(body, 3)
		body = binary.LittleEndian.AppendUint16(body, uint16(len(block)))
		body = binary.LittleEndian.AppendUint32(body, checksum(block))
		body = append(body, block...)
	}
	return body
}

func (p *peer) ready(t *testing.T) {
	t.Helper()
	reply, err := p.exchange(moduleRequest())
	if err != nil || !bytes.Equal(reply, []byte{1}) {
		t.Fatalf("module: %x %v", reply, err)
	}
	reply, err = p.exchange(append([]byte{5}, moduleSeed...))
	if err != nil || !bytes.Equal(reply, append([]byte{4}, moduleHash...)) {
		t.Fatalf("hash: %x %v", reply, err)
	}
	p.send, _ = rc4.NewCipher(serverKey)
	p.recv, _ = rc4.NewCipher(clientKey)
	reply, err = p.exchange(initRequest())
	if err != nil || reply != nil {
		t.Fatalf("init: %x %v", reply, err)
	}
}

func TestEncryptedHandshakeAndRepeatedChecks(t *testing.T) {
	p := newPeer()
	p.ready(t)
	// Empty string table, timing, a stock memory check, module scan, terminator.
	// Encode independently to make the byte layout legible.
	body := []byte{2, 0, 0x57 ^ 0x7f, 0xf3 ^ 0x7f, 0}
	body = binary.LittleEndian.AppendUint32(body, 9995315)
	body = append(body, 5, 0xd9^0x7f)
	body = append(body, make([]byte, 24)...)
	body = append(body, 0x7f)
	for i := 0; i < 5; i++ {
		reply, err := p.exchange(body)
		if err != nil {
			t.Fatal(err)
		}
		if len(reply) != 19 || reply[0] != 2 || binary.LittleEndian.Uint16(reply[1:]) != 12 || binary.LittleEndian.Uint32(reply[3:]) != checksum(reply[7:]) {
			t.Fatalf("result envelope: %x", reply)
		}
		if reply[7] != 1 || !bytes.Equal(reply[12:], fromHex("0075440fb75ee9")) {
			t.Fatalf("result data: %x", reply)
		}
	}
}

func TestHandshakeValidation(t *testing.T) {
	for _, tc := range []struct {
		name    string
		body    []byte
		advance bool
		want    string
	}{
		{"empty", nil, false, "size"},
		{"oversize", make([]byte, maxPayload+1), false, "size"},
		{"short module", []byte{0}, false, "module request"},
		{"unknown module", append([]byte{0}, make([]byte, 36)...), false, "unsupported Warden module"},
		{"early hash", append([]byte{5}, moduleSeed...), false, "hash request"},
		{"wrong seed", append([]byte{5}, make([]byte, 16)...), true, "module seed"},
		{"early checks", []byte{2, 0, 0x28, 0x7f}, false, "before module initialization"},
		{"module cache", []byte{1, 0, 0}, false, "unsupported Warden command"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := newPeer()
			if tc.advance {
				if _, err := p.exchange(moduleRequest()); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := p.exchange(tc.body); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatal(err)
			}
		})
	}
	for _, offset := range []int{0, 1, 3, 7, 30, 54} {
		p := newPeer()
		p.exchange(moduleRequest())
		p.exchange(append([]byte{5}, moduleSeed...))
		p.send, _ = rc4.NewCipher(serverKey)
		body := initRequest()
		body[offset] ^= 0x80
		if _, err := p.exchange(body); err == nil {
			t.Errorf("accepted bad init at %d", offset)
		}
	}
}

func TestChecks(t *testing.T) {
	lua := "local S,T,R=SendAddonMessage,function()return not not PQR_IsMoving end R=S and T()if R then S('_TW',0789,'GUILD')end"
	body := append([]byte{byte(len(lua))}, []byte(lua)...)
	body = append(body, 3, 'd', 'r', 'v', 0, 0x57^0x7f, 0x8b^0x7f, 1, 0x71^0x7f)
	body = append(body, make([]byte, 24)...)
	body = append(body, 2, 0xb2^0x7f)
	body = append(body, make([]byte, 28)...)
	body = append(body, 4, 0xbf^0x7f)
	body = append(body, make([]byte, 28)...)
	body = append(body, 4, 0x7f)
	result, err := checks(body, 0x12345678)
	if err != nil || !bytes.Equal(result, fromHex("01785634120000e9e9e9")) {
		t.Fatalf("%x %v", result, err)
	}
	for n := 0; n < len(body); n++ {
		if _, err := checks(body[:n], 0); err == nil {
			t.Fatalf("accepted truncation at %d", n)
		}
	}
	for _, request := range [][]byte{
		{0, 0x28, 0x98 ^ 0x7f, 1, 0x7f},                // MPQ
		{0, 0x28, 0xf3 ^ 0x7f, 0, 1, 0, 0, 0, 2, 0x7f}, // unknown memory
		{0, 0x28, 0x8b ^ 0x7f, 1, 0x7f},                // invalid string index
		{0, 0x28, 0x7e},                                // wrong terminator
		{0, 0x28, 0x7f, 0x7f},                          // trailing bytes
	} {
		if _, err := checks(request, 0); err == nil {
			t.Fatalf("accepted %x", request)
		}
	}
	if supportedLua(strings.ReplaceAll(lua, "PQR_IsMoving", "customFunction()")) {
		t.Fatal("accepted arbitrary Lua")
	}
	for address, expected := range memoryProfile {
		request := binary.LittleEndian.AppendUint32([]byte{0, 0x28, 0xf3 ^ 0x7f, 0}, address)
		request = append(request, byte(len(expected)), 0x7f)
		result, err := checks(request, 1)
		if err != nil || !bytes.Equal(result[6:], expected) {
			t.Fatalf("memory %x: %x %v", address, result, err)
		}
	}
}

func FuzzWardenChecks(f *testing.F) {
	f.Add([]byte{0, 0x28, 0x7f})
	f.Fuzz(func(t *testing.T, body []byte) {
		if len(body) > maxPayload {
			return
		}
		result, _ := checks(body, 0)
		if len(result) > maxPayload {
			t.Fatal("unbounded result")
		}
	})
}
