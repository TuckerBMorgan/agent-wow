package world

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/modules/session"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type observedRuntime struct {
	modrt.Runtime
	session modsession.Session
	packets chan packet
	start   func(context.Context) error
	prepare func(context.Context) error
	flush   func(context.Context) error
}

func (r *observedRuntime) Start(ctx context.Context, h modsession.Session) error {
	r.session = h
	if err := r.Runtime.Start(ctx, h); err != nil {
		return err
	}
	if r.start != nil {
		return r.start(ctx)
	}
	return nil
}
func (r *observedRuntime) Publish(op uint16, b []byte) error {
	if r.packets != nil {
		r.packets <- packet{opcode: op, body: bytes.Clone(b)}
	}
	return nil
}
func (r *observedRuntime) PrepareLogout(ctx context.Context) error {
	if r.prepare != nil {
		return r.prepare(ctx)
	}
	return r.Runtime.PrepareLogout(ctx)
}
func (r *observedRuntime) FlushPackets(ctx context.Context) error {
	if r.flush != nil {
		return r.flush(ctx)
	}
	return r.Runtime.FlushPackets(ctx)
}
func newObserved() *observedRuntime {
	return &observedRuntime{Runtime: modrt.New(nil, nil, nil), packets: make(chan packet, 128)}
}

func TestGameplayPacketsRemainOpaque(t *testing.T) {
	r := newObserved()
	ops := []uint16{opcode.SMSGForceMoveRoot, opcode.SMSGClientControlUpdate, opcode.MSGMoveTeleportAck, opcode.SMSGTransferPending, opcode.SMSGNewWorld, opcode.SMSGTriggerCinematic, opcode.SMSGTriggerMovie, opcode.SMSGCompressedMoves}
	trigger, delivered := make(chan struct{}), make(chan struct{})
	c := peerConnection(t, func(p *testPeer) {
		p.login()
		<-trigger
		for i, op := range ops {
			p.send(op, []byte{byte(i), 0xff})
		}
		p.sync(99)
		close(delivered)
		p.closed()
	})
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	close(trigger)
	<-delivered // expect() inside sync rejects any automatic gameplay response.
	got := map[uint16][]byte{}
	for {
		select {
		case p := <-r.packets:
			got[p.opcode] = p.body
			if p.opcode == opcode.SMSGTimeSyncReq && binary.LittleEndian.Uint32(p.body) == 99 {
				goto complete
			}
		case <-time.After(time.Second):
			t.Fatal("missing routed packet")
		}
	}
complete:
	for i, op := range ops {
		if !bytes.Equal(got[op], []byte{byte(i), 0xff}) {
			t.Fatal(op, got[op])
		}
	}
	if len(got[opcode.SMSGLoginVerifyWorld]) != 20 {
		t.Fatal("core packet was not observed")
	}
}

func TestModulePreparationCanWriteAndClockMatchesSync(t *testing.T) {
	r := newObserved()
	var mu sync.Mutex
	var syncTime uint32
	r.prepare = func(ctx context.Context) error {
		return r.session.SendPacket(ctx, opcode.CMSGQueryTime, []byte("stop"))
	}
	c := peerConnection(t, func(p *testPeer) {
		p.expect(opcode.CMSGPlayerLogin)
		p.send(opcode.SMSGLoginVerifyWorld, positionBody(0, 1, 2, 3, 0))
		p.send(opcode.SMSGTimeSyncReq, []byte{9, 0, 0, 0})
		body := p.expect(opcode.CMSGTimeSyncResp)
		mu.Lock()
		syncTime = binary.LittleEndian.Uint32(body[4:])
		mu.Unlock()
		if got := p.expect(opcode.CMSGQueryTime); !bytes.Equal(got, []byte("stop")) {
			t.Fatal(got)
		}
		p.expect(opcode.CMSGLogoutRequest)
		p.send(opcode.SMSGLogoutComplete, nil)
		p.closed()
	})
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err := s.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if delta := r.session.Clock() - syncTime; delta > 1000 {
		t.Fatalf("module clock differs from time sync by %dms", delta)
	}
}

func TestFinalPacketFlushKeepsCallbackWritesAvailable(t *testing.T) {
	r := newObserved()
	r.flush = func(ctx context.Context) error {
		return r.session.SendPacket(ctx, opcode.CMSGQueryTime, []byte("final acknowledgement"))
	}
	c := peerConnection(t, func(p *testPeer) {
		p.login()
		p.expect(opcode.CMSGLogoutRequest)
		p.send(opcode.SMSGLogoutComplete, nil)
		if got := p.expect(opcode.CMSGQueryTime); string(got) != "final acknowledgement" {
			t.Fatal(got)
		}
		p.closed()
	})
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err := s.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestModuleWriteHonorsCallerDeadline(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	c := peerConnection(t, func(p *testPeer) { p.login(); <-release })
	timing := quietTimings()
	timing.write = 10 * time.Second
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, timing)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := s.SendPacket(ctx, opcode.CMSGQueryTime, nil); err == nil {
		t.Fatal("blocked transport write succeeded")
	}
	select {
	case <-s.Done():
		if s.Err() == nil {
			t.Fatal("failed transport write did not end session")
		}
	case <-time.After(time.Second):
		t.Fatal("owner write ignored caller deadline")
	}
}

func TestClockWrap(t *testing.T) {
	s := &Session{started: time.Now().Add(-((1 << 32) + 100) * time.Millisecond)}
	if clock := s.Clock(); clock < 100 || clock > 110 {
		t.Fatal(clock)
	}
}

func TestAutonomousWritesAreSerializedAndValidated(t *testing.T) {
	r := newObserved()
	r.start = func(ctx context.Context) error {
		if err := r.session.SendPacket(ctx, opcode.CMSGQueryTime, nil); status.Code(err) != codes.FailedPrecondition {
			return errors.New("accepted packet before login")
		}
		return nil
	}
	c := peerConnection(t, func(p *testPeer) {
		p.login()
		seen := map[byte]bool{}
		for range 16 {
			body := p.expect(opcode.CMSGQueryTime)
			if len(body) != 1 || seen[body[0]] {
				t.Fatal("corrupt or repeated write", body)
			}
			seen[body[0]] = true
		}
		p.expect(opcode.CMSGLogoutRequest)
		p.send(opcode.SMSGLogoutComplete, nil)
		p.closed()
	})
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	for _, op := range []uint32{opcode.CMSGPlayerLogin, opcode.CMSGLogoutRequest, opcode.CMSGTimeSyncResp, opcode.CMSGWardenData, opcode.CMSGAuthSRP6Begin, opcode.CMSGAuthSRP6Proof, opcode.CMSGAuthSRP6Recode, opcode.CMSGRedirectionAuthProof, opcode.CMSGCheckLoginCriteria} {
		if err := s.SendPacket(context.Background(), op, nil); status.Code(err) != codes.PermissionDenied {
			t.Fatal(err)
		}
	}
	if err := s.SendPacket(context.Background(), opcode.SMSGPong, nil); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	if err := s.SendPacket(context.Background(), opcode.CMSGQueryTime, make([]byte, 10236)); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := s.SendPacket(context.Background(), opcode.CMSGQueryTime, []byte{byte(i)}); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if err := s.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := s.SendPacket(context.Background(), opcode.CMSGQueryTime, nil); status.Code(err) != codes.FailedPrecondition {
		t.Fatal(err)
	}
}

func TestModuleStartupMaintainsTransportAndDelaysLogin(t *testing.T) {
	r := newObserved()
	maintained := make(chan struct{})
	r.start = func(ctx context.Context) error {
		select {
		case <-maintained:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	timing := quietTimings()
	timing.ping = 20 * time.Millisecond
	timing.pong = time.Second
	c := peerConnection(t, func(p *testPeer) {
		body := p.expect(opcode.CMSGPing)
		p.send(opcode.SMSGPong, body[:4])
		close(maintained)
		p.login()
		p.closed()
	})
	s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, timing, Options{Modules: r, ModuleTimeout: time.Second, EntryTimeout: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	s.Close()
}

func TestModuleStartupAndPreparationFailureEndSession(t *testing.T) {
	t.Run("startup", func(t *testing.T) {
		r := newObserved()
		r.start = func(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }
		c := peerConnection(t, func(p *testPeer) { p.closed() })
		_, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r, ModuleTimeout: 20 * time.Millisecond})
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
	})
	t.Run("preparation", func(t *testing.T) {
		r := newObserved()
		r.prepare = func(context.Context) error { return errors.New("cannot stop module") }
		c := peerConnection(t, func(p *testPeer) { p.login(); p.closed() })
		s, err := enter(context.Background(), c, Realm{}, Character{99, "Mira"}, nil, quietTimings(), Options{Modules: r})
		if err != nil {
			t.Fatal(err)
		}
		defer s.Close()
		if err := s.Logout(context.Background()); err == nil || !strings.Contains(err.Error(), "cannot stop") {
			t.Fatal(err)
		}
	})
}
