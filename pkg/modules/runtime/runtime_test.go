package modrt

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/internal/modulefixture"
	fixturev1 "github.com/agent-wow/agent-wow/internal/modulefixture/api"
	"github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/modules/internal/testutil"
	"github.com/agent-wow/agent-wow/pkg/modules/runner"
	"github.com/agent-wow/agent-wow/pkg/modules/session"
	"github.com/agent-wow/agent-wow/pkg/modules/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/emptypb"
)

type runningFixture struct {
	service  *modulefixture.Service
	server   *grpc.Server
	health   *health.Server
	callback *grpc.ClientConn
	launch   modrunner.Launch
}
type fixtureRunner struct {
	mu        sync.Mutex
	fixtures  map[string]*runningFixture
	events    []string
	configure func(string, *modulefixture.Service)
	failUp    string
	startup   func(context.Context, modrunner.Launch) error
}

func (r *fixtureRunner) Up(ctx context.Context, l modrunner.Launch) error {
	r.mu.Lock()
	r.events = append(r.events, "up:"+l.Manifest.Name)
	r.mu.Unlock()
	if r.startup != nil {
		if err := r.startup(ctx, l); err != nil {
			return err
		}
	}
	if l.Manifest.Name == r.failUp {
		return errors.New("injected up failure")
	}
	listener, err := net.Listen("unix", filepath.Join(l.SocketDir, "module.sock"))
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient("unix://"+filepath.Join(l.SocketDir, "session.sock"), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	if err != nil {
		listener.Close()
		return err
	}
	s := &modulefixture.Service{Session: modv1.NewSessionClient(conn)}
	if len(l.Manifest.Requires) > 0 {
		s.Dependency = l.Manifest.Requires[0]
	}
	if r.configure != nil {
		r.configure(l.Manifest.Name, s)
	}
	server := grpc.NewServer()
	fixturev1.RegisterFixtureServer(server, s)
	healthy := health.NewServer()
	grpc_health_v1.RegisterHealthServer(server, healthy)
	healthy.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	r.mu.Lock()
	if r.fixtures == nil {
		r.fixtures = map[string]*runningFixture{}
	}
	r.fixtures[l.Manifest.Name] = &runningFixture{s, server, healthy, conn, l}
	r.mu.Unlock()
	go server.Serve(listener)
	return nil
}
func (r *fixtureRunner) Down(_ context.Context, l modrunner.Launch) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, "down:"+l.Manifest.Name)
	if f := r.fixtures[l.Manifest.Name]; f != nil {
		f.server.Stop()
		f.callback.Close()
		f.service.StopBackground()
	}
	return nil
}
func startManager(t *testing.T, r *fixtureRunner, send func(context.Context, uint32, []byte) error) *Manager {
	t.Helper()
	return startGraph(t, r, map[string]string{"orchestrator": "worker", "worker": ""}, send)
}
func startGraph(t *testing.T, r *fixtureRunner, graph map[string]string, send func(context.Context, uint32, []byte) error) *Manager {
	t.Helper()
	root := t.TempDir()
	for name, requires := range graph {
		modtest.WriteModule(t, root, name, requires)
	}
	registry, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	m := New(registry, r, nil)
	m.healthInterval = 20 * time.Millisecond
	m.timeHealth = 100 * time.Millisecond
	t.Cleanup(func() {
		if err := m.Close(); err != nil {
			t.Error(err)
		}
	})
	if send == nil {
		send = func(context.Context, uint32, []byte) error { return nil }
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := m.Start(ctx, modsession.Session{SendPacket: send, Clock: func() uint32 { return 12345 }}); err != nil {
		t.Fatal(err)
	}
	return m
}
func waitFailure(t *testing.T, m *Manager) {
	t.Helper()
	select {
	case <-m.Done():
		if m.Err() == nil {
			t.Fatal("missing module failure")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("module failure not detected")
	}
}

func TestTypedCompositionAndPublicValidation(t *testing.T) {
	r := &fixtureRunner{}
	sent := make(chan *modv1.SendPacketRequest, 10)
	m := startManager(t, r, func(_ context.Context, op uint32, b []byte) error {
		sent <- &modv1.SendPacketRequest{Opcode: op, Payload: bytes.Clone(b)}
		return nil
	})
	body, err := m.InvokeJSON(context.Background(), "orchestrator.execute", json.RawMessage(`{"number":"9007199254740993","payload":"AQID","opcode":123}`))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(body, []byte(`"9007199254740993"`)) || !bytes.Contains(body, []byte(`"AQID"`)) || !bytes.Contains(body, []byte(`12345`)) {
		t.Fatal(string(body))
	}
	if p := <-sent; p.Opcode != 123 || !bytes.Equal(p.Payload, []byte{1, 2, 3}) {
		t.Fatal(p)
	}
	for _, tc := range []struct {
		method, params string
		code           int
	}{
		{"worker.missing", "{}", -32601}, {"session.getState", "{}", -32601}, {"disabled.execute", "{}", -32601},
		{"worker.execute", "[]", -32602}, {"worker.execute", `{"unknown":1}`, -32602}, {"worker.execute", `{"number":"-1"}`, -32602},
		{"worker.execute", `{"failure":"expected gameplay error"}`, -32000},
	} {
		_, err := m.InvokeJSON(context.Background(), tc.method, json.RawMessage(tc.params))
		var call *CallError
		if !errors.As(err, &call) || call.Code != tc.code {
			t.Fatalf("%s: %v", tc.params, err)
		}
	}
	if m.Err() != nil {
		t.Fatal("ordinary application error killed session", m.Err())
	}
	for _, params := range []json.RawMessage{nil, json.RawMessage("null"), json.RawMessage("{}")} {
		if _, err := m.InvokeJSON(context.Background(), "worker.execute", params); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := m.InvokeJSON(ctx, "orchestrator.execute", json.RawMessage(`{"delayMs":500}`)); status.Code(err) != codes.DeadlineExceeded {
		t.Fatal(err)
	}
	input, _ := anypb.New(&fixturev1.Request{})
	worker := r.fixtures["worker"].service.Session
	if _, err := worker.InvokeModule(context.Background(), &modv1.InvokeModuleRequest{Module: "orchestrator", Method: "execute", Request: input}); status.Code(err) != codes.PermissionDenied {
		t.Fatal(err)
	}
	orchestrator := r.fixtures["orchestrator"].service.Session
	wrong, _ := anypb.New(&emptypb.Empty{})
	if _, err := orchestrator.InvokeModule(context.Background(), &modv1.InvokeModuleRequest{Module: "worker", Method: "execute", Request: wrong}); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	if _, err := orchestrator.InvokeModule(context.Background(), &modv1.InvokeModuleRequest{Module: "worker", Method: "OnPacket", Request: input}); status.Code(err) != codes.NotFound {
		t.Fatal(err)
	}
}

func TestPacketFanoutAndCallbacks(t *testing.T) {
	var mu sync.Mutex
	received := map[string][]byte{}
	packetDone := make(chan struct{}, 8)
	sendDone := make(chan struct{}, 8)
	r := &fixtureRunner{configure: func(name string, s *modulefixture.Service) {
		s.PacketReplyOpcode = 123
		s.Packet = func(_ context.Context, p *modv1.WorldPacket) error {
			mu.Lock()
			received[name] = append(received[name], p.Payload...)
			mu.Unlock()
			packetDone <- struct{}{}
			return nil
		}
	}}
	m := startManager(t, r, func(context.Context, uint32, []byte) error { sendDone <- struct{}{}; return nil })
	for i := byte(0); i < 4; i++ {
		payload := []byte{i}
		if err := m.Publish(0x236, payload); err != nil {
			t.Fatal(err)
		}
		payload[0] = 99
	}
	for range 8 {
		select {
		case <-packetDone:
		case <-time.After(time.Second):
			t.Fatal("packet stalled")
		}
		select {
		case <-sendDone:
		case <-time.After(time.Second):
			t.Fatal("callback stalled")
		}
	}
	mu.Lock()
	defer mu.Unlock()
	for name, got := range received {
		if !bytes.Equal(got, []byte{0, 1, 2, 3}) {
			t.Fatal(name, got)
		}
	}
}

func TestLogoutOrderCompositionAndResume(t *testing.T) {
	var mu sync.Mutex
	var order []string
	r := &fixtureRunner{configure: func(name string, s *modulefixture.Service) {
		s.Before = func(context.Context) error { mu.Lock(); order = append(order, name); mu.Unlock(); return nil }
	}}
	m := startManager(t, r, nil)
	m.BeginLogout()
	if _, err := m.InvokeJSON(context.Background(), "worker.execute", nil); status.Code(err) != codes.FailedPrecondition {
		t.Fatal(err)
	}
	if err := m.PrepareLogout(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(order, []string{"orchestrator", "worker"}) {
		t.Fatal(order)
	}
	input, _ := anypb.New(&fixturev1.Request{})
	if _, err := r.fixtures["orchestrator"].service.Session.InvokeModule(context.Background(), &modv1.InvokeModuleRequest{Module: "worker", Method: "execute", Request: input}); status.Code(err) != codes.FailedPrecondition {
		t.Fatal(err)
	}
	if _, err := r.fixtures["worker"].service.Session.GetClock(context.Background(), &emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	m.Resume()
	if _, err := m.InvokeJSON(context.Background(), "orchestrator.execute", nil); err != nil {
		t.Fatal(err)
	}
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(r.events, []string{"up:worker", "up:orchestrator", "down:orchestrator", "down:worker"}) {
		t.Fatal(r.events)
	}
}

func TestFatalFailuresAndCleanup(t *testing.T) {
	for _, mode := range []string{"transport", "health", "unavailable", "packet", "hook", "hook timeout"} {
		t.Run(mode, func(t *testing.T) {
			r := &fixtureRunner{configure: func(name string, s *modulefixture.Service) {
				if name != "worker" {
					return
				}
				if mode == "packet" {
					s.Packet = func(context.Context, *modv1.WorldPacket) error { return errors.New("bad packet") }
				}
				if mode == "hook" {
					s.Before = func(context.Context) error { return errors.New("hook failure") }
				}
				if mode == "hook timeout" {
					s.Before = func(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }
				}
			}}
			m := startManager(t, r, nil)
			switch mode {
			case "transport":
				r.fixtures["worker"].server.Stop()
			case "health":
				r.fixtures["worker"].health.SetServingStatus("", grpc_health_v1.HealthCheckResponse_NOT_SERVING)
			case "unavailable":
				_, _ = m.InvokeJSON(context.Background(), "worker.execute", json.RawMessage(`{"failure":"unavailable"}`))
			case "packet":
				if err := m.Publish(0x236, []byte{1}); err != nil {
					t.Fatal(err)
				}
			case "hook", "hook timeout":
				m.BeginLogout()
				ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
				defer cancel()
				if err := m.PrepareLogout(ctx); err == nil {
					t.Fatal("hook succeeded")
				}
			}
			waitFailure(t, m)
			if !strings.Contains(m.Err().Error(), "worker") {
				t.Fatal(m.Err())
			}
		})
	}
	t.Run("partial startup", func(t *testing.T) {
		root := t.TempDir()
		modtest.WriteModule(t, root, "worker", "")
		modtest.WriteModule(t, root, "orchestrator", "worker")
		registry, err := moddisc.Load(root)
		if err != nil {
			t.Fatal(err)
		}
		r := &fixtureRunner{failUp: "orchestrator"}
		m := New(registry, r, nil)
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := m.Start(ctx, modsession.Session{SendPacket: func(context.Context, uint32, []byte) error { return nil }, Clock: func() uint32 { return 0 }}); err == nil {
			t.Fatal("startup succeeded")
		}
		if err := m.Close(); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(r.events, []string{"up:worker", "up:orchestrator", "down:orchestrator", "down:worker"}) {
			t.Fatal(r.events)
		}
		if m.Err() != nil {
			t.Fatal("intentional cleanup became a health failure", m.Err())
		}
	})
}

func TestPacketQueueLimits(t *testing.T) {
	root := t.TempDir()
	modtest.WriteModule(t, root, "worker", "")
	registry, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, size := range []int{1, 1 << 20} {
		m := New(registry, nil, nil)
		count := queueLimit
		if size > 1 {
			count = queueBytes / size
		}
		for range count {
			if err := m.Publish(0x236, make([]byte, size)); err != nil {
				t.Fatal(err)
			}
		}
		if err := m.Publish(0x236, make([]byte, size)); err == nil {
			t.Fatal("queue grew without bound")
		}
		m.Stop()
	}
}
