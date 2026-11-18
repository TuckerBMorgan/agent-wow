package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/internal/config"
	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/world"
	"github.com/agent-wow/agent-wow/pkg/worldrpc"
)

type fakePlaySession struct {
	mu     sync.Mutex
	state  world.State
	done   chan struct{}
	finish sync.Once
	logout func(context.Context) error
}

func newFakePlaySession() *fakePlaySession {
	return &fakePlaySession{state: world.State{Status: world.InWorld, Realm: world.Realm{ID: 7, Name: "Test"}, Character: world.Character{GUID: 9007199254740993, Name: "Mira"}}, done: make(chan struct{})}
}
func (s *fakePlaySession) Snapshot() world.State { s.mu.Lock(); defer s.mu.Unlock(); return s.state }
func (s *fakePlaySession) InvokeJSON(context.Context, string, json.RawMessage) (json.RawMessage, error) {
	return nil, &modrt.CallError{Code: -32601, Err: errors.New("Method not found")}
}
func (s *fakePlaySession) Logout(ctx context.Context) error {
	if s.logout != nil {
		return s.logout(ctx)
	}
	return s.Close()
}
func (s *fakePlaySession) Done() <-chan struct{} { return s.done }
func (s *fakePlaySession) Err() error            { return nil }
func (s *fakePlaySession) Close() error {
	s.finish.Do(func() { s.mu.Lock(); s.state.Status = world.Closed; s.mu.Unlock(); close(s.done) })
	return nil
}
func (s *fakePlaySession) setStatus(status world.Status) {
	s.mu.Lock()
	s.state.Status = status
	s.mu.Unlock()
}

type readyWriter struct{ messages chan []byte }

func (w readyWriter) Write(p []byte) (int, error) { w.messages <- bytes.Clone(p); return len(p), nil }

func readyURL(t *testing.T, messages <-chan []byte) string {
	t.Helper()
	select {
	case body := <-messages:
		// These fields contain no spaces or quoting; other attributes may do so.
		fields := make(map[string]string)
		for _, field := range strings.Fields(string(body)) {
			if key, value, ok := strings.Cut(field, "="); ok {
				fields[key] = value
			}
		}
		pid, err := strconv.Atoi(fields["pid"])
		if fields["level"] != "INFO" || !bytes.Contains(body, []byte(`msg="world session ready"`)) || fields["event"] != "ready" || err != nil || pid <= 0 || fields["character.guid"] != "9007199254740993" {
			t.Fatal(string(body))
		}
		return fields["rpc_url"]
	case <-time.After(2 * time.Second):
		t.Fatal("no readiness record")
		return ""
	}
}
func postRPC(t *testing.T, url, method string) []byte {
	t.Helper()
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Post(url, "application/json", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"`+method+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatal(resp.Status, string(body))
	}
	return body
}
func TestPlayFlushesLogoutResponse(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	s := newFakePlaySession()
	out := readyWriter{make(chan []byte, 1)}
	logger := slog.New(slog.NewTextHandler(out, nil))
	exited := make(chan error, 1)
	go func() { exited <- servePlay(context.Background(), listener, s, logger) }()
	url := readyURL(t, out.messages)
	if body := postRPC(t, url, "session.getState"); !bytes.Contains(body, []byte(`"code":-32601`)) {
		t.Fatal(string(body))
	}
	if body := postRPC(t, url, "session.logout"); !bytes.Contains(body, []byte(`"result":{"status":"closed"}`)) {
		t.Fatal("lost final reply", string(body))
	}
	select {
	case err := <-exited:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("server did not stop")
	}
}
func TestPlayCancellationGracefullyLogsOut(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	s := newFakePlaySession()
	called, finish := make(chan struct{}), make(chan struct{})
	s.logout = func(ctx context.Context) error {
		s.setStatus(world.LoggingOut)
		close(called)
		if ctx.Err() != nil {
			t.Error("graceful logout inherited canceled context")
		}
		<-finish
		return s.Close()
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	out := readyWriter{make(chan []byte, 1)}
	logger := slog.New(slog.NewTextHandler(out, nil))
	exited := make(chan error, 1)
	go func() { exited <- servePlay(ctx, listener, s, logger) }()
	url := readyURL(t, out.messages)
	cancel()
	<-called
	if body := postRPC(t, url, "session.getState"); !bytes.Contains(body, []byte(`"code":-32601`)) {
		t.Fatal(string(body))
	}
	close(finish)
	if err := <-exited; err != nil {
		t.Fatal(err)
	}
}
func TestPlayCommandStartupTimeoutDoesNotLimitGameplay(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	t.Setenv("AGENT_WOW_WORLDRPC_HOST", "127.0.0.1")
	t.Setenv("AGENT_WOW_WORLDRPC_PORT", strconv.Itoa(listener.Addr().(*net.TCPAddr).Port))
	realm, saved := setupCharacterRealm(t)
	s := newFakePlaySession()
	s.state.Realm = world.Realm{ID: realm.ID, Name: realm.Name}
	var startup context.Context
	deps := characterCommands{play: func(ctx context.Context, r auth.Realm, a *auth.Session, selector string, logger *slog.Logger, opts world.Options) (playSession, error) {
		if r != realm || *a != *saved || selector != "Mira" || logger == nil {
			t.Error("wrong startup arguments")
		}
		startup = ctx
		return s, nil
	}}
	command := deps.command()
	command.SetArgs([]string{"play", "Mira", "--timeout", "100ms"})
	out := readyWriter{make(chan []byte, 1)}
	var stdout bytes.Buffer
	command.SetOut(&stdout)
	command.SetErr(out)
	exited := make(chan error, 1)
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	go func() { exited <- command.Execute() }()
	url := readyURL(t, out.messages)
	if startup.Err() == nil {
		t.Fatal("startup context not released")
	}
	time.Sleep(120 * time.Millisecond)
	postRPC(t, url, "session.getState")
	postRPC(t, url, "session.logout")
	if err := <-exited; err != nil {
		t.Fatal(err)
	}
	if stdout.Len() != 0 {
		t.Fatalf("unexpected stdout: %s", stdout.String())
	}
}
func TestPlayReservesListenerBeforeConnecting(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	t.Setenv("AGENT_WOW_WORLDRPC_HOST", "127.0.0.1")
	t.Setenv("AGENT_WOW_WORLDRPC_PORT", strconv.Itoa(listener.Addr().(*net.TCPAddr).Port))
	initTestConfig(t)
	command := characterCommands{play: func(context.Context, auth.Realm, *auth.Session, string, *slog.Logger, world.Options) (playSession, error) {
		t.Error("entered world before binding listener")
		return nil, errors.New("unexpected")
	}}.command()
	command.SetArgs([]string{"play", "Mira"})
	command.SetErr(io.Discard)
	if err := command.Execute(); err == nil || !strings.Contains(err.Error(), "address already in use") {
		t.Fatal(err)
	}
}

func TestPlayLoadsConfiguredModuleDirectory(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	t.Setenv("AGENT_WOW_WORLDRPC_HOST", "127.0.0.1")
	t.Setenv("AGENT_WOW_WORLDRPC_PORT", strconv.Itoa(listener.Addr().(*net.TCPAddr).Port))
	path := initTestConfig(t)
	root := t.TempDir()
	t.Setenv("AGENT_WOW_MODULE_DIR", root)
	if err := config.Init(path); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, "invalid")
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "module.yaml"), []byte("api_version: 999\nenabled: true\n"), 0600); err != nil {
		t.Fatal(err)
	}
	command := characterCommands{play: func(context.Context, auth.Realm, *auth.Session, string, *slog.Logger, world.Options) (playSession, error) {
		t.Error("entered world before validating modules")
		return nil, errors.New("unexpected")
	}}.command()
	command.SetArgs([]string{"play", "Mira"})
	command.SetErr(io.Discard)
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	if err := command.Execute(); err == nil || !strings.Contains(err.Error(), "module invalid: unsupported api_version 999") {
		t.Fatalf("expected validation of configured module directory, got %v", err)
	}
}

func TestPlayCommandListenerConfig(t *testing.T) {
	for _, tc := range []struct {
		name, host, envHost, wantErr string
	}{
		{name: "configured occupied port", host: "localhost", wantErr: "address already in use"},
		{name: "configured non-loopback host", host: "0.0.0.0", wantErr: "loopback"},
		{name: "environment overrides configuration", host: "0.0.0.0", envHost: "127.0.0.1", wantErr: "address already in use"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			// Cobra constructs commands before the root command loads config.
			command := characterCommands{play: func(context.Context, auth.Realm, *auth.Session, string, *slog.Logger, world.Options) (playSession, error) {
				t.Error("entered world before validating/binding the RPC listener")
				return nil, errors.New("unexpected")
			}}.command()
			t.Setenv("AGENT_WOW_WORLDRPC_HOST", tc.envHost)
			t.Setenv("AGENT_WOW_WORLDRPC_PORT", "")
			path := initTestConfig(t)
			body, err := json.Marshal(map[string]any{
				"worldrpc": config.Server{Host: tc.host, Port: listener.Addr().(*net.TCPAddr).Port},
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, body, 0600); err != nil {
				t.Fatal(err)
			}
			if err := config.Init(path); err != nil {
				t.Fatal(err)
			}
			command.SetArgs([]string{"play", "Mira"})
			command.SetErr(io.Discard)
			if err := command.Execute(); err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("expected %q, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestPlayArgumentValidation(t *testing.T) {
	for _, args := range [][]string{{"play"}, {"play", " "}, {"play", "Mira", "--timeout", "0"}} {
		command := characterCommands{}.command()
		command.SetArgs(args)
		command.SetErr(io.Discard)
		if err := command.Execute(); err == nil {
			t.Fatal("accepted", args)
		}
	}
}

func TestPlayDoesNotAnnounceEndedSession(t *testing.T) {
	listener, err := worldrpc.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	s := newFakePlaySession()
	s.Close()
	var out bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&out, nil))
	if err := servePlay(context.Background(), listener, s, logger); err == nil {
		t.Fatal("announced a closed session")
	}
	if out.Len() != 0 {
		t.Fatal(out.String())
	}
}
