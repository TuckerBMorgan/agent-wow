package worldconn

import (
	"bytes"
	"crypto/rc4"
	"net"
	"testing"
)

type packetPeer struct {
	conn net.Conn
	send *rc4.Cipher
}

func (p *packetPeer) write(opcode uint16, body []byte) error {
	size := len(body) + 2
	header := []byte{}
	if size > 0x7fff {
		header = append(header, 0x80|byte(size>>16))
	}
	header = append(header, byte(size>>8), byte(size), byte(opcode), byte(opcode>>8))
	if p.send != nil {
		p.send.XORKeyStream(header, header)
	}
	// Deliberately fragment headers and payloads; no dependency on our encoder.
	for _, b := range append(header, body...) {
		if _, err := p.conn.Write([]byte{b}); err != nil {
			return err
		}
	}
	return nil
}

func TestExtendedHeadersAndPartialWrites(t *testing.T) {
	var key [40]byte
	for i := range key {
		key[i] = byte(i + 1)
	}
	left, right := net.Pipe()
	defer left.Close()
	defer right.Close()
	c := &Conn{conn: left, recv: HeaderCipher(key, false)}
	p := &packetPeer{conn: right, send: HeaderCipher(key, false)}
	done := make(chan error, 1)
	go func() {
		if err := p.write(0x03b, bytes.Repeat([]byte{42}, 0x8000)); err != nil {
			done <- err
			return
		}
		done <- p.write(0x03a, []byte{0x2f})
	}()
	for _, size := range []int{0x8000, 1} {
		_, body, err := c.ReadPacket()
		if err != nil || len(body) != size {
			t.Fatal(len(body), err)
		}
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	recorder := &shortWriteConn{}
	c = &Conn{conn: recorder}
	if sent, err := c.WritePacket(0x038, []byte{1, 2}); err != nil || !sent {
		t.Fatal(err)
	}
	if !bytes.Equal(recorder.Bytes(), []byte{0, 6, 0x38, 0, 0, 0, 1, 2}) {
		t.Fatal("short writes lost bytes", recorder.Bytes())
	}
}

type shortWriteConn struct {
	net.Conn
	bytes.Buffer
}

func (c *shortWriteConn) Read(p []byte) (int, error) { return c.Buffer.Read(p) }

func (c *shortWriteConn) Write(p []byte) (int, error) { return c.Buffer.Write(p[:1]) }
