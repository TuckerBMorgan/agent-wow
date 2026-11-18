package world

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/worldconn"
)

// This peer independently encodes the build-12340 headers and fixture payloads.
// The character tests additionally exercise the same session with encryption.
type testPeer struct {
	t    *testing.T
	conn net.Conn
}

func (p *testPeer) send(op uint16, body []byte) {
	p.t.Helper()
	size := len(body) + 2
	h := []byte{byte(size >> 8), byte(size), byte(op), byte(op >> 8)}
	if size > 0x7fff {
		h = append([]byte{0x80 | byte(size>>16)}, h...)
	}
	if _, err := p.conn.Write(append(h, body...)); err != nil {
		p.t.Fatalf("send 0x%x: %v", op, err)
	}
}
func (p *testPeer) read() (uint32, []byte, error) {
	var h [6]byte
	if _, err := io.ReadFull(p.conn, h[:]); err != nil {
		return 0, nil, err
	}
	n := int(binary.BigEndian.Uint16(h[:2])) - 4
	if n < 0 {
		return 0, nil, errors.New("invalid client header")
	}
	body := make([]byte, n)
	_, err := io.ReadFull(p.conn, body)
	return binary.LittleEndian.Uint32(h[2:]), body, err
}
func (p *testPeer) expect(op uint32) []byte {
	p.t.Helper()
	got, body, err := p.read()
	if err != nil || got != op {
		p.t.Fatalf("read: got 0x%x, want 0x%x: %v", got, op, err)
	}
	return body
}
func (p *testPeer) closed() {
	p.t.Helper()
	_, _, err := p.read()
	if err == nil {
		p.t.Fatal("unexpected packet instead of close")
	}
}
func positionBody(mapID uint32, xyz ...float32) []byte {
	body := binary.LittleEndian.AppendUint32(nil, mapID)
	for _, f := range xyz {
		body = binary.LittleEndian.AppendUint32(body, math.Float32bits(f))
	}
	return body
}
func (p *testPeer) login() {
	if body := p.expect(0x03d); !bytes.Equal(body, []byte{99, 0, 0, 0, 0, 0, 0, 0}) {
		p.t.Fatal("wrong login", body)
	}
	p.send(0x236, positionBody(0, 1, 2, 3, 0.5))
	p.sync(5)
}
func (p *testPeer) sync(counter uint32) {
	p.send(0x390, binary.LittleEndian.AppendUint32(nil, counter))
	body := p.expect(0x391)
	if len(body) != 8 || binary.LittleEndian.Uint32(body) != counter {
		p.t.Fatal("bad time response", body)
	}
}
func quietTimings() timings {
	return timings{time.Hour, time.Hour, time.Second, 200 * time.Millisecond}
}
func peerConnection(t *testing.T, script func(*testPeer)) *worldconn.Conn {
	t.Helper()
	left, right := net.Pipe()
	done := make(chan struct{})
	right.SetDeadline(time.Now().Add(4 * time.Second))
	go func() { defer close(done); defer right.Close(); script(&testPeer{t, right}) }()
	t.Cleanup(func() { left.Close(); right.Close(); <-done })
	return worldconn.New(left)
}
func startTestSession(t *testing.T, timing timings, script func(*testPeer)) *Session {
	t.Helper()
	c := peerConnection(t, script)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	s, err := enter(ctx, c, Realm{1, "Test"}, Character{99, "Mira"}, nil, timing)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}
func awaitDone(t *testing.T, s *Session) {
	t.Helper()
	select {
	case <-s.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("session did not terminate")
	}
}

func TestReadinessNeedsLoginVerificationAndAnsweredSync(t *testing.T) {
	for _, syncFirst := range []bool{false, true} {
		t.Run(map[bool]string{false: "location first", true: "sync first"}[syncFirst], func(t *testing.T) {
			first, release := make(chan struct{}), make(chan struct{})
			c := peerConnection(t, func(p *testPeer) {
				p.expect(0x03d)
				if syncFirst {
					p.sync(1)
				} else {
					p.send(0x236, positionBody(0, 1, 2, 3, 0.5))
				}
				close(first)
				<-release
				if syncFirst {
					p.send(0x236, positionBody(0, 1, 2, 3, 0.5))
				} else {
					p.sync(1)
				}
				p.closed()
			})
			type result struct {
				s   *Session
				err error
			}
			ready := make(chan result, 1)
			go func() {
				s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings())
				ready <- result{s, err}
			}()
			<-first
			select {
			case r := <-ready:
				t.Fatalf("early readiness: %v", r.err)
			case <-time.After(10 * time.Millisecond):
			}
			close(release)
			r := <-ready
			if r.err != nil {
				t.Fatal(r.err)
			}
			if r.s.Snapshot().Status != InWorld {
				t.Fatal(r.s.Snapshot())
			}
			r.s.Close()
		})
	}
}

func TestEntryCancellationAndRejection(t *testing.T) {
	t.Run("cancellation", func(t *testing.T) {
		c := peerConnection(t, func(p *testPeer) { p.expect(0x03d); p.closed() })
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		_, err := enter(ctx, c, Realm{}, Character{99, "Mira"}, nil, quietTimings())
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
	})
	t.Run("rejection", func(t *testing.T) {
		c := peerConnection(t, func(p *testPeer) { p.expect(0x03d); p.send(0x041, []byte{0x50}); p.closed() })
		_, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings())
		var rejected *LoginError
		if !errors.As(err, &rejected) || rejected.Code != 0x50 {
			t.Fatal(err)
		}
	})
}

func TestHeartbeatsAndMissingPongs(t *testing.T) {
	healthy := make(chan struct{})
	timing := timings{20 * time.Millisecond, 200 * time.Millisecond, time.Second, 200 * time.Millisecond}
	s := startTestSession(t, timing, func(p *testPeer) {
		p.login()
		for i := uint32(1); i <= 12; i++ {
			body := p.expect(0x1dc)
			if len(body) != 8 || binary.LittleEndian.Uint32(body) != i {
				t.Fatal("wrong ping", body)
			}
			p.send(0x1dd, body[:4])
		}
		close(healthy)
		// Duplicate/unknown pongs and unrelated traffic cannot keep a dead link alive.
		for {
			op, _, err := p.read()
			if err != nil {
				return
			}
			if op != 0x1dc {
				t.Fatalf("unexpected packet 0x%x", op)
			}
			// The watchdog may close the pipe during this final bogus reply.
			if _, err := p.conn.Write([]byte{0, 6, 0xdd, 1, 0xff, 0xff, 0xff, 0xff}); err != nil {
				return
			}
		}
	})
	<-healthy
	if s.Snapshot().Status != InWorld {
		t.Fatal("lost a healthy session")
	}
	awaitDone(t, s)
	if s.Err() == nil || !strings.Contains(s.Err().Error(), "pong timeout") || s.Snapshot().Status != Failed {
		t.Fatal(s.Err())
	}
}

func TestSharedLogoutSurvivesCallerCancellation(t *testing.T) {
	requested, finish := make(chan struct{}), make(chan struct{})
	s := startTestSession(t, quietTimings(), func(p *testPeer) {
		p.login()
		p.expect(0x04b)
		p.send(0x04c, []byte{0, 0, 0, 0, 0})
		close(requested)
		<-finish
		p.send(0x04d, nil)
		p.closed()
	})
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { first <- s.Logout(ctx) }()
	<-requested
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if s.Snapshot().Status != LoggingOut {
		t.Fatal(s.Snapshot())
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := s.Logout(context.Background()); err != nil {
				t.Error(err)
			}
		}()
	}
	close(finish)
	wg.Wait()
	awaitDone(t, s)
	if err := s.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
	if s.Snapshot().Status != Closed {
		t.Fatal(s.Snapshot())
	}
}

func TestRejectedThenImmediateLogout(t *testing.T) {
	s := startTestSession(t, quietTimings(), func(p *testPeer) {
		p.login()
		p.expect(0x04b)
		p.send(0x04c, []byte{1, 0, 0, 0, 0})
		p.expect(0x04b)
		p.send(0x04c, []byte{0, 0, 0, 0, 1})
		p.send(0x04d, nil)
		p.closed()
	})
	var rejected *LogoutError
	if err := s.Logout(context.Background()); !errors.As(err, &rejected) || rejected.Reason != 1 {
		t.Fatal(err)
	}
	if s.Snapshot().Status != InWorld {
		t.Fatal(s.Snapshot())
	}
	if err := s.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitDone(t, s)
}

func TestLogoutTimeoutAndWriteFailure(t *testing.T) {
	t.Run("logout timeout", func(t *testing.T) {
		timing := quietTimings()
		timing.logout = 30 * time.Millisecond
		s := startTestSession(t, timing, func(p *testPeer) { p.login(); p.expect(0x04b); p.send(0x04c, []byte{0, 0, 0, 0, 0}); p.closed() })
		if err := s.Logout(context.Background()); err == nil || !strings.Contains(err.Error(), "logout timed out") {
			t.Fatal(err)
		}
		awaitDone(t, s)
	})
	t.Run("blocked write", func(t *testing.T) {
		finish := make(chan struct{})
		defer close(finish)
		timing := quietTimings()
		timing.ping = 10 * time.Millisecond
		timing.write = 20 * time.Millisecond
		s := startTestSession(t, timing, func(p *testPeer) { p.login(); <-finish })
		awaitDone(t, s)
		var nerr net.Error
		if !errors.As(s.Err(), &nerr) || !nerr.Timeout() {
			t.Fatal(s.Err())
		}
	})
}

func TestMalformedPacketsAndDisconnect(t *testing.T) {
	for _, tc := range []struct {
		name string
		op   uint16
		body []byte
	}{
		{"short location", 0x236, []byte{1}},
		{"short sync", 0x390, []byte{1}}, {"short pong", 0x1dd, nil},
		{"bad logout", 0x04c, []byte{0, 0, 0, 0, 9}}, {"bad complete", 0x04d, []byte{1}},
		{"warden", 0x2e6, []byte{1}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			trigger := make(chan struct{})
			s := startTestSession(t, quietTimings(), func(p *testPeer) { p.login(); <-trigger; p.send(tc.op, tc.body); p.closed() })
			close(trigger)
			awaitDone(t, s)
			if s.Err() == nil || s.Snapshot().Status != Failed {
				t.Fatal(s.Snapshot())
			}
			if tc.name == "warden" && !strings.Contains(s.Err().Error(), "Warden") {
				t.Fatal(s.Err())
			}
		})
	}
	t.Run("disconnect", func(t *testing.T) {
		disconnect := make(chan struct{})
		s := startTestSession(t, quietTimings(), func(p *testPeer) { p.login(); <-disconnect })
		close(disconnect)
		awaitDone(t, s)
		if !errors.Is(s.Err(), io.EOF) {
			t.Fatal(s.Err())
		}
	})
}

func TestLogoutBeforeReadiness(t *testing.T) {
	c := peerConnection(t, func(p *testPeer) { p.expect(0x03d); p.send(0x04d, nil); p.closed() })
	_, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings())
	if err == nil || !strings.Contains(err.Error(), "during entry") {
		t.Fatal(err)
	}
}

func TestCloseJoinsSessionAndDoesNotConfirmLogout(t *testing.T) {
	s := startTestSession(t, quietTimings(), func(p *testPeer) { p.login(); p.closed() })
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() { defer wg.Done(); s.Close() }()
	}
	wg.Wait()
	awaitDone(t, s)
	if !errors.Is(s.Err(), ErrClosed) || !errors.Is(s.Logout(context.Background()), ErrClosed) {
		t.Fatal(s.Err())
	}
	if s.Snapshot().Status != Closed {
		t.Fatal(s.Snapshot())
	}
}

func TestExpiredPongDoesNotRenewLiveness(t *testing.T) {
	timer := time.NewTimer(0)
	defer timer.Stop()
	e := engine{timing: timings{pong: time.Second}, pongTimer: timer, pings: map[uint32]time.Time{1: time.Now().Add(-2 * time.Second)}}
	if _, err := e.handle(0x1dd, []byte{1, 0, 0, 0}); err != nil {
		t.Fatal(err)
	}
	if len(e.pings) != 0 {
		t.Fatal("retained expired ping")
	}
	select {
	case <-timer.C:
	case <-time.After(20 * time.Millisecond):
		t.Fatal("expired pong renewed the watchdog")
	}
}
