package char

import (
	"bytes"
	"context"
	"crypto/rc4"
	"crypto/sha1"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/worldconn"
)

type realmPeer struct {
	conn         net.Conn
	send, recv   *rc4.Cipher
	beforeAuthOK func() error
}

func (p *realmPeer) read() (uint32, []byte, error) {
	var h [6]byte
	if _, err := io.ReadFull(p.conn, h[:]); err != nil {
		return 0, nil, err
	}
	if p.recv != nil {
		p.recv.XORKeyStream(h[:], h[:])
	}
	size := int(binary.BigEndian.Uint16(h[:2]))
	if size < 4 || size >= 10240 {
		return 0, nil, fmt.Errorf("bad client size %d", size)
	}
	body := make([]byte, size-4)
	_, err := io.ReadFull(p.conn, body)
	return binary.LittleEndian.Uint32(h[2:]), body, err
}

func (p *realmPeer) expect(opcode uint32) ([]byte, error) {
	op, body, err := p.read()
	if err == nil && op != opcode {
		err = fmt.Errorf("got opcode 0x%x, want 0x%x", op, opcode)
	}
	return body, err
}

func (p *realmPeer) write(opcode uint16, body []byte) error {
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

func (p *realmPeer) authenticate(session *auth.Session, queued bool) error {
	challenge := make([]byte, 40)
	binary.LittleEndian.PutUint32(challenge, 1)
	copy(challenge[4:8], []byte{0x12, 0x34, 0x56, 0x78})
	if err := p.write(0x1ec, challenge); err != nil {
		return err
	}
	body, err := p.expect(0x1ed)
	if err != nil {
		return err
	}
	usernameEnd := 8 + len(session.Username)
	if len(body) != usernameEnd+1+4+4+4+4+4+8+20+4 {
		return fmt.Errorf("wrong auth body size %d", len(body))
	}
	if binary.LittleEndian.Uint32(body) != 12340 || binary.LittleEndian.Uint32(body[4:]) != 0 || string(body[8:usernameEnd]) != session.Username || body[usernameEnd] != 0 {
		return errors.New("wrong build, login ID or account")
	}
	tail := body[usernameEnd+1:]
	if binary.LittleEndian.Uint32(tail) != 0 || binary.LittleEndian.Uint32(tail[8:]) != 0 || binary.LittleEndian.Uint32(tail[12:]) != 0 || binary.LittleEndian.Uint32(tail[16:]) != 7 || binary.LittleEndian.Uint64(tail[20:]) != 0 || binary.LittleEndian.Uint32(tail[48:]) != 0 {
		return errors.New("wrong auth fields or addon block")
	}
	h := sha1.New()
	h.Write([]byte(session.Username))
	h.Write(make([]byte, 4))
	h.Write(tail[4:8])
	h.Write(challenge[4:8])
	h.Write(session.Key[:])
	if !bytes.Equal(h.Sum(nil), tail[28:48]) {
		return errors.New("wrong world authentication proof")
	}
	p.send, p.recv = worldconn.HeaderCipher(session.Key, false), worldconn.HeaderCipher(session.Key, true)
	if err := p.write(0x209, []byte{1, 2, 3}); err != nil {
		return err
	} // Interleaved unrelated startup packet.
	if p.beforeAuthOK != nil {
		if err := p.beforeAuthOK(); err != nil {
			return err
		}
	}
	response := make([]byte, 11)
	response[0], response[10] = 0x0c, 2
	if queued {
		response[0] = 0x1b
		response = append(binary.LittleEndian.AppendUint32(response, 4), 0)
		if err := p.write(0x1ee, response); err != nil {
			return err
		}
		if err := p.write(0x1ee, []byte{0x1b, 1, 0, 0, 0, 0}); err != nil {
			return err
		}
		return p.write(0x1ee, []byte{0x0c})
	}
	return p.write(0x1ee, response)
}

func testSession() *auth.Session {
	s := &auth.Session{Username: "PLAYER"}
	for i := range s.Key {
		s.Key[i] = byte(i)
	}
	return s
}

func testRealm(t *testing.T, handler func(*realmPeer) error) auth.Realm {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		conn, err := l.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(5 * time.Second))
		done <- handler(&realmPeer{conn: conn})
	}()
	t.Cleanup(func() {
		l.Close()
		if err := <-done; err != nil {
			t.Error("fake worldserver:", err)
		}
	})
	return auth.Realm{ID: 7, Name: "Test Realm", Address: l.Addr().String()}
}

func characterFixture(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile("testdata/characters.bin")
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestClientLifecycle(t *testing.T) {
	fixture := characterFixture(t)
	session := testSession()
	realm := testRealm(t, func(p *realmPeer) error {
		if err := p.authenticate(session, true); err != nil {
			return err
		}
		if body, err := p.expect(0x037); err != nil || len(body) != 0 {
			return fmt.Errorf("enum request: %v", err)
		}
		if err := p.write(0x4ab, []byte{1, 0, 0, 0}); err != nil {
			return err
		}
		if err := p.write(0x03b, fixture); err != nil {
			return err
		}
		if _, err := p.expect(0x037); err != nil {
			return err
		}
		if err := p.write(0x03b, fixture); err != nil {
			return err
		}
		var previous []byte
		for i := 0; i < 2; i++ {
			body, err := p.expect(0x036)
			if err != nil {
				return err
			}
			nul := bytes.IndexByte(body, 0)
			if nul != 12 || len(body[nul+1:]) != 9 {
				return errors.New("incorrect creation layout")
			}
			if i == 0 {
				previous = bytes.Clone(body)
			} else if bytes.Equal(previous[:bytes.IndexByte(previous, 0)], body[:nul]) || !bytes.Equal(previous[len(previous)-9:], body[len(body)-9:]) {
				return errors.New("name retry changed appearance or failed to change name")
			}
			code := byte(0x32)
			if i == 1 {
				code = 0x2f
			}
			if err := p.write(0x03a, []byte{code}); err != nil {
				return err
			}
		}
		if _, err := p.expect(0x037); err != nil {
			return err
		}
		if err := p.write(0x03b, fixture); err != nil {
			return err
		}
		body, err := p.expect(0x038)
		if err != nil {
			return err
		}
		if !bytes.Equal(body, []byte{99, 0, 0, 0, 0, 0, 0, 0}) {
			return errors.New("incorrect deletion GUID")
		}
		return p.write(0x03c, []byte{0x47})
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	c, err := Dial(ctx, realm, session)
	if err != nil {
		t.Fatal(err)
	}
	cancel() // Dial's context must not own the connection after success.
	defer c.Close()
	c.choose = rand.New(rand.NewPCG(123, 456)).IntN
	ctx, cancel = context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	characters, err := c.List(ctx)
	if err != nil || len(characters) != 2 || characters[0].GUID != 9007199254740993 {
		t.Fatal(characters, err)
	}
	created, err := c.Create(ctx, CreateOptions{Race: ptr(RaceHuman), Class: ptr(ClassWarrior), Gender: ptr(GenderMale), Skin: ptr(uint8(0))})
	if err != nil || created.Name == "" || created.Appearance.Skin != 0 {
		t.Fatal(created, err)
	}
	if err := c.Delete(ctx, 99); err != nil {
		t.Fatal(err)
	}
	if c.expansion != 2 {
		t.Fatal("lost expansion in queue updates")
	}
}

func TestCreateRejectionsAndUnknownOutcome(t *testing.T) {
	for _, tc := range []struct {
		name       string
		code       byte
		explicit   bool
		attempts   int
		disconnect bool
	}{
		{"explicit name", 0x32, true, 1, false}, {"generated collision bound", 0x32, false, 5, false}, {"generated invalid name", 0x5f, false, 5, false},
		{"account limit", 0x36, false, 1, false}, {"disconnected mutation", 0, false, 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := testSession()
			realm := testRealm(t, func(p *realmPeer) error {
				if err := p.authenticate(session, false); err != nil {
					return err
				}
				if _, err := p.expect(0x037); err != nil {
					return err
				}
				if err := p.write(0x03b, []byte{0}); err != nil {
					return err
				}
				for range tc.attempts {
					if _, err := p.expect(0x036); err != nil {
						return err
					}
					if tc.disconnect {
						return nil
					}
					if err := p.write(0x03a, []byte{tc.code}); err != nil {
						return err
					}
				}
				_, _, err := p.read()
				if !errors.Is(err, io.EOF) {
					return fmt.Errorf("unexpected retry: %v", err)
				}
				return nil
			})
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			c, err := Dial(ctx, realm, session)
			if err != nil {
				t.Fatal(err)
			}
			defer c.Close()
			o := CreateOptions{}
			if tc.explicit {
				o.Name = ptr("Arlen")
			}
			_, err = c.Create(ctx, o)
			if tc.disconnect {
				var unknown *OutcomeUnknownError
				if !errors.As(err, &unknown) || unknown.Operation != "create" || unknown.Name == "" {
					t.Fatal("expected uncertain creation", err)
				}
			} else {
				var rejection *ServerError
				if !errors.As(err, &rejection) || rejection.Code != tc.code {
					t.Fatal("expected rejection", err)
				}
			}
		})
	}
}

func TestDeleteOwnershipAndFailures(t *testing.T) {
	fixture := characterFixture(t)
	for _, tc := range []struct {
		name    string
		guid    GUID
		code    byte
		unknown bool
	}{
		{"not owned", 100, 0, false}, {"guild leader", 99, 0x4a, false}, {"arena captain", 99, 0x4b, false}, {"disconnected", 99, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := testSession()
			realm := testRealm(t, func(p *realmPeer) error {
				if err := p.authenticate(session, false); err != nil {
					return err
				}
				if _, err := p.expect(0x037); err != nil {
					return err
				}
				if err := p.write(0x03b, fixture); err != nil {
					return err
				}
				if tc.guid == 100 {
					_, _, err := p.read()
					if !errors.Is(err, io.EOF) {
						return errors.New("sent an unowned deletion")
					}
					return nil
				}
				if _, err := p.expect(0x038); err != nil {
					return err
				}
				if tc.unknown {
					return nil
				}
				return p.write(0x03c, []byte{tc.code})
			})
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			c, err := Dial(ctx, realm, session)
			if err != nil {
				t.Fatal(err)
			}
			defer c.Close()
			err = c.Delete(ctx, tc.guid)
			if tc.guid == 100 {
				if err == nil || !strings.Contains(err.Error(), "not in this account") {
					t.Fatal(err)
				}
			} else if tc.unknown {
				var u *OutcomeUnknownError
				if !errors.As(err, &u) || u.GUID != 99 {
					t.Fatal(err)
				}
			} else {
				var e *ServerError
				if !errors.As(err, &e) || e.Code != tc.code {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestCancellationAndMutationTimeout(t *testing.T) {
	for _, mutation := range []bool{false, true} {
		t.Run(fmt.Sprint(mutation), func(t *testing.T) {
			session := testSession()
			received := make(chan struct{})
			realm := testRealm(t, func(p *realmPeer) error {
				if err := p.authenticate(session, false); err != nil {
					return err
				}
				if _, err := p.expect(0x037); err != nil {
					return err
				}
				if mutation {
					if err := p.write(0x03b, []byte{0}); err != nil {
						return err
					}
					if _, err := p.expect(0x036); err != nil {
						return err
					}
				}
				close(received)
				_, err := io.Copy(io.Discard, p.conn)
				return err
			})
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			c, err := Dial(ctx, realm, session)
			if err != nil {
				t.Fatal(err)
			}
			defer c.Close()
			opCtx, stop := context.WithCancel(context.Background())
			defer stop()
			go func() { <-received; stop() }()
			if mutation {
				_, err = c.Create(opCtx, CreateOptions{})
			} else {
				_, err = c.List(opCtx)
			}
			if !errors.Is(err, context.Canceled) {
				t.Fatal("missing context cause", err)
			}
			if mutation {
				var u *OutcomeUnknownError
				if !errors.As(err, &u) {
					t.Fatal("mutation was not uncertain", err)
				}
			}
		})
	}
}

func TestHeaderCipherVectors(t *testing.T) {
	// Independent Python HMAC-SHA1 + RC4 implementation, key bytes 0..39,
	// zero plaintext after discarding the first 1024 stream bytes.
	for sending, want := range map[bool]string{true: "e65788e6a6ce478550d66e87b5c31ede4a5737a122f9ad1a", false: "da4770c4efd0bceb7173cb145169e13d03a7c1d1c8813006"} {
		cipher := worldconn.HeaderCipher(testSession().Key, sending)
		var got [24]byte
		cipher.XORKeyStream(got[:7], got[:7])
		cipher.XORKeyStream(got[7:], got[7:])
		if hex.EncodeToString(got[:]) != want {
			t.Fatal("wrong cipher direction or stream position", sending, hex.EncodeToString(got[:]))
		}
	}
}

func TestAuthenticationFailures(t *testing.T) {
	for _, tc := range []struct {
		name            string
		beforeChallenge bool
		body            []byte
		code            byte
	}{
		{"IP ban", true, []byte{0x1c}, 0x1c},
		{"expired session", false, []byte{0x17}, 0x17},
		{"empty response", false, nil, 0},
		{"truncated success", false, []byte{0x0c, 0}, 0},
		{"truncated queue", false, []byte{0x1b, 0}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := testSession()
			realm := testRealm(t, func(p *realmPeer) error {
				if !tc.beforeChallenge {
					if err := p.write(0x1ec, make([]byte, 40)); err != nil {
						return err
					}
					if _, err := p.expect(0x1ed); err != nil {
						return err
					}
					p.send = worldconn.HeaderCipher(session.Key, false)
				}
				return p.write(0x1ee, tc.body)
			})
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			client, err := Dial(ctx, realm, session)
			if err == nil || client != nil {
				t.Fatal("accepted failed authentication")
			}
			if tc.code != 0 {
				var rejection *ServerError
				if !errors.As(err, &rejection) || rejection.Code != tc.code || rejection.Operation != "auth" {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestPacketBoundsAndDeadline(t *testing.T) {
	for _, header := range [][]byte{
		{0, 1, 0x3b, 0},       // size smaller than the opcode
		{0x90, 0, 1, 0x3b, 0}, // extended size larger than the allocation limit
		{0, 4, 0x3b, 0, 1},    // truncated body
		{0x80, 0},             // truncated extended header
	} {
		left, right := net.Pipe()
		c := &Client{wire: worldconn.New(left)}
		done := make(chan struct{})
		go func() { defer close(done); defer right.Close(); _, _ = right.Write(header) }()
		if _, _, err := c.readPacket(); err == nil {
			t.Fatal("accepted malformed header", header)
		}
		left.Close()
		<-done
	}
	left, right := net.Pipe()
	defer left.Close()
	defer right.Close()
	c := &Client{wire: worldconn.New(left)}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := c.List(ctx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("lost deadline cause", err)
	}
}

func TestCharacterDecoder(t *testing.T) {
	fixture := characterFixture(t)
	chars, err := decodeCharacters(fixture)
	if err != nil || len(chars) != 2 {
		t.Fatal(chars, err)
	}
	if chars[0] != (Character{GUID: 9007199254740993, Name: "Arlen", Race: RaceHuman, Class: ClassWarrior, Gender: GenderMale, Level: 60, ZoneID: 12, MapID: 0, Appearance: Appearance{2, 3, 4, 5, 6}}) || chars[1].Name != "Mira" || chars[1].MapID != 530 {
		t.Fatal(chars)
	}
	for i := 0; i < len(fixture); i++ {
		if _, err := decodeCharacters(fixture[:i]); err == nil {
			t.Fatalf("accepted truncation at %d", i)
		}
	}
	for _, change := range []func([]byte) []byte{
		func(p []byte) []byte { return append(p, 0) }, func(p []byte) []byte { p[0] = 255; return p }, func(p []byte) []byte { p[9] = 0x1b; return p },
		func(p []byte) []byte { clear(p[1:9]); return p }, func(p []byte) []byte { copy(p[276:284], p[1:9]); return p },
	} {
		if _, err := decodeCharacters(change(bytes.Clone(fixture))); err == nil {
			t.Fatal("accepted malformed record")
		}
	}
	chars, err = decodeCharacters([]byte{0})
	if err != nil || chars == nil || len(chars) != 0 {
		t.Fatal("empty list", chars, err)
	}
}

func FuzzDecodeCharacters(f *testing.F) {
	fixture, err := os.ReadFile("testdata/characters.bin")
	if err != nil {
		f.Fatal(err)
	}
	f.Add(fixture)
	f.Add([]byte{0})
	f.Add([]byte{255})
	f.Fuzz(func(t *testing.T, data []byte) { decodeCharacters(data) })
}
