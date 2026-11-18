package modrt

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
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
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/anypb"
)

func TestStartupCanInvokeHealthyDependency(t *testing.T) {
	r := &fixtureRunner{startup: func(ctx context.Context, l modrunner.Launch) error {
		if l.Manifest.Name != "orchestrator" {
			return nil
		}
		conn, err := grpc.NewClient("unix://"+filepath.Join(l.SocketDir, "session.sock"), grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return err
		}
		defer conn.Close()
		input, _ := anypb.New(&fixturev1.Request{})
		_, err = modv1.NewSessionClient(conn).InvokeModule(ctx, &modv1.InvokeModuleRequest{Module: "worker", Method: "execute", Request: input})
		return err
	}}
	startManager(t, r, nil)
}

func TestLogoutSettlesCallsAndRejectsPreparedTargets(t *testing.T) {
	entered := make(chan struct{}, 1)
	r := &fixtureRunner{}
	r.configure = func(name string, s *modulefixture.Service) {
		if name != "worker" {
			return
		}
		s.Call = func(_ context.Context, req *fixturev1.Request) error {
			if req.DelayMs != 0 {
				entered <- struct{}{}
			}
			return nil
		}
		s.Before = func(ctx context.Context) error {
			input, _ := anypb.New(&fixturev1.Request{})
			_, err := r.fixtures["app"].service.Session.InvokeModule(ctx, &modv1.InvokeModuleRequest{Module: "orchestrator", Method: "execute", Request: input})
			if status.Code(err) != codes.FailedPrecondition {
				t.Errorf("call to prepared dependency during preparation: %v", err)
			}
			return nil
		}
	}
	m := startGraph(t, r, map[string]string{"app": "orchestrator", "orchestrator": "worker", "worker": ""}, nil)
	finished := make(chan error, 1)
	go func() {
		_, err := m.InvokeJSON(context.Background(), "worker.execute", json.RawMessage(`{"delayMs":10000}`))
		finished <- err
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("call did not arrive")
	}
	m.BeginLogout()
	if err := m.PrepareLogout(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := <-finished; status.Code(err) != codes.Canceled {
		t.Fatal("hook did not cancel in-flight operation", err)
	}
	m.Resume()
	if _, err := m.InvokeJSON(context.Background(), "app.execute", nil); err != nil {
		t.Fatal("new operations unavailable after rejected logout", err)
	}
}

func TestPacketsBufferedBeforeStartupAndFlushed(t *testing.T) {
	root := t.TempDir()
	modtest.WriteModule(t, root, "worker", "")
	registry, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	handled := make(chan byte, 1)
	r := &fixtureRunner{configure: func(_ string, s *modulefixture.Service) {
		s.Packet = func(_ context.Context, p *modv1.WorldPacket) error { handled <- p.Payload[0]; return nil }
	}}
	m := New(registry, r, nil)
	defer m.Close()
	if err := m.Publish(0x236, []byte{9}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := m.Start(ctx, modsession.Session{SendPacket: func(context.Context, uint32, []byte) error { return nil }, Clock: func() uint32 { return 0 }}); err != nil {
		t.Fatal(err)
	}
	if err := m.FlushPackets(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case b := <-handled:
		if b != 9 {
			t.Fatal(b)
		}
	default:
		t.Fatal("flush returned before handler completed")
	}
}

func TestSessionResourceIsolation(t *testing.T) {
	r1, r2 := &fixtureRunner{}, &fixtureRunner{}
	m1, m2 := startManager(t, r1, nil), startManager(t, r2, nil)
	for _, name := range []string{"worker", "orchestrator"} {
		a, b := r1.fixtures[name].launch, r2.fixtures[name].launch
		if a.Project == b.Project || a.SocketDir == b.SocketDir {
			t.Fatal("sessions share a project or socket directory")
		}
		for path, mode := range map[string]os.FileMode{filepath.Dir(a.SocketDir): 0700, a.SocketDir: 0777, filepath.Join(a.SocketDir, "session.sock"): 0666} {
			st, err := os.Stat(path)
			if err != nil || st.Mode().Perm() != mode {
				t.Fatal(path, st, err)
			}
		}
	}
	if err := m1.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(m1.dir); !os.IsNotExist(err) {
		t.Fatal("runtime directory not removed", err)
	}
	if _, err := m2.InvokeJSON(context.Background(), "orchestrator.execute", nil); err != nil {
		t.Fatal("cleanup affected another session", err)
	}
}
