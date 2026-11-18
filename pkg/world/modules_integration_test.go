package world_test

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/modules/runner"
	"github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	"github.com/agent-wow/agent-wow/pkg/world"
	"github.com/agent-wow/agent-wow/pkg/worldconn"
	"github.com/agent-wow/agent-wow/pkg/worldrpc"
)

// TestDockerModuleSession deliberately uses only its own Compose projects and a
// fake world peer. Opt in with AGENT_WOW_DOCKER_TEST=1; no running realm is used.
func TestDockerModuleSession(t *testing.T) {
	if os.Getenv("AGENT_WOW_DOCKER_TEST") != "1" {
		t.Skip("set AGENT_WOW_DOCKER_TEST=1 to exercise Docker Compose")
	}
	base := t.TempDir()
	repo, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	binaryPath := filepath.Join(base, "fixture")
	build := exec.Command("go", "build", "-o", binaryPath, "./internal/modulefixture/cmd")
	build.Dir = repo
	build.Env = append(os.Environ(), "CGO_ENABLED=0", "GOOS=linux")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build fixture: %v\n%s", err, out)
	}
	for _, mode := range []string{"logout", "module failure"} {
		t.Run(mode, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "modules")
			for _, name := range []string{"worker", "orchestrator"} {
				dir := filepath.Join(root, name)
				if err := os.MkdirAll(dir, 0755); err != nil {
					t.Fatal(err)
				}
				for _, pair := range [][2]string{{binaryPath, filepath.Join(dir, "fixture")}, {filepath.Join(repo, "internal/modulefixture/Dockerfile"), filepath.Join(dir, "Dockerfile")}, {filepath.Join(repo, "internal/modulefixture/fixture.pb"), filepath.Join(dir, "module.pb")}} {
					b, err := os.ReadFile(pair[0])
					if err != nil {
						t.Fatal(err)
					}
					if err := os.WriteFile(pair[1], b, 0755); err != nil {
						t.Fatal(err)
					}
				}
				requires := "[]"
				env := ""
				if name == "orchestrator" {
					requires = "[worker]"
					env = "    environment:\n      FIXTURE_DEPENDENCY: worker\n"
				}
				compose := "services:\n  module:\n    build: .\n" + env
				manifest := `api_version: 1
enabled: true
requires: ` + requires + `
compose:
  file: compose.yaml
  service: module
grpc:
  descriptor_set: module.pb
rpc:
  execute: /agentwow.fixture.v1.Fixture/Execute
packets:
  SMSG_LOGIN_VERIFY_WORLD: /agentwow.fixture.v1.Fixture/OnPacket
lifecycle:
  before_logout: /agentwow.fixture.v1.Fixture/BeforeLogout
`
				for file, body := range map[string]string{"compose.yaml": compose, "module.yaml": manifest} {
					if err := os.WriteFile(filepath.Join(dir, file), []byte(body), 0644); err != nil {
						t.Fatal(err)
					}
				}
			}
			registry, err := moddisc.Load(root)
			if err != nil {
				t.Fatal(err)
			}
			runner := &recordCompose{}
			manager := modrt.New(registry, runner, nil)
			t.Cleanup(func() {
				if err := manager.Close(); err != nil {
					t.Error(err)
				}
			})
			left, right := net.Pipe()
			defer left.Close()
			defer right.Close()
			sent := make(chan []byte, 1)
			peerErr := make(chan error, 1)
			go func() { defer right.Close(); peerErr <- integrationPeer(right, sent) }()
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancel()
			s, err := world.Enter(ctx, worldconn.New(left), world.Realm{ID: 1, Name: "Fixture"}, world.Character{GUID: 99, Name: "Mira"}, nil, world.Options{Modules: manager, ModuleTimeout: time.Minute, EntryTimeout: time.Second})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := s.Close(); err != nil {
					t.Error(err)
				}
			})
			invoke := func(method string, params json.RawMessage) (json.RawMessage, error) {
				body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
				req := httptest.NewRequest("POST", "http://127.0.0.1:8086/rpc", bytes.NewReader(body))
				req.Header.Set("Content-Type", "application/json")
				recorder := httptest.NewRecorder()
				worldrpc.Handler(s).ServeHTTP(recorder, req)
				var reply struct {
					Result json.RawMessage
					Error  *struct{ Message string }
				}
				if err := json.Unmarshal(recorder.Body.Bytes(), &reply); err != nil {
					return nil, err
				}
				if reply.Error != nil {
					return nil, fmt.Errorf("RPC: %s", reply.Error.Message)
				}
				return reply.Result, nil
			}
			var response json.RawMessage
			deadline := time.Now().Add(2 * time.Second)
			for {
				response, err = invoke("orchestrator.execute", json.RawMessage(`{"number":"9007199254740993"}`))
				if err != nil {
					t.Fatal(err)
				}
				var state struct{ Packets uint32 }
				if err := json.Unmarshal(response, &state); err != nil {
					t.Fatal(err)
				}
				if state.Packets == 1 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("initial packet was not routed", string(response))
				}
				time.Sleep(10 * time.Millisecond)
			}
			response, err = invoke("orchestrator.execute", json.RawMessage(fmt.Sprintf(`{"number":"9007199254740993","opcode":%d,"payload":"AQID"}`, opcode.CMSGQueryTime)))
			if err != nil || !bytes.Contains(response, []byte(`"9007199254740993"`)) {
				t.Fatal(string(response), err)
			}
			select {
			case body := <-sent:
				if !bytes.Equal(body, []byte{1, 2, 3}) {
					t.Fatal(body)
				}
			case <-time.After(time.Second):
				t.Fatal("module did not send packet")
			}
			if mode == "logout" {
				if _, err := invoke("session.logout", nil); err != nil {
					t.Fatal(err)
				}
			} else {
				l := runner.launches["worker"]
				cmd := exec.CommandContext(ctx, "docker", "compose", "--project-name", l.Project, "--project-directory", l.Manifest.Directory, "-f", filepath.Join(l.Manifest.Directory, l.Manifest.Compose.File), "-f", l.Override, "kill", l.Manifest.Compose.Service)
				if out, err := cmd.CombinedOutput(); err != nil {
					t.Fatalf("stop fixture: %v %s", err, out)
				}
				select {
				case <-s.Done():
					if s.Err() == nil || !strings.Contains(s.Err().Error(), "worker") {
						t.Fatal(s.Err())
					}
				case <-time.After(5 * time.Second):
					t.Fatal("module failure did not end session")
				}
			}
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			if err := <-peerErr; err != nil {
				t.Fatal(err)
			}
			for _, l := range runner.launches {
				out, err := exec.Command("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project="+l.Project).CombinedOutput()
				if err != nil || len(bytes.TrimSpace(out)) != 0 {
					t.Fatalf("left fixture containers: %s %v", out, err)
				}
			}
		})
	}
}

type recordCompose struct {
	modrunner.ComposeRunner
	launches map[string]modrunner.Launch
}

func (r *recordCompose) Up(ctx context.Context, l modrunner.Launch) error {
	if r.launches == nil {
		r.launches = map[string]modrunner.Launch{}
	}
	r.launches[l.Manifest.Name] = l
	return r.ComposeRunner.Up(ctx, l)
}

func integrationPeer(conn net.Conn, sent chan<- []byte) error {
	write := func(op uint16, b []byte) error {
		h := binary.BigEndian.AppendUint16(nil, uint16(len(b)+2))
		h = binary.LittleEndian.AppendUint16(h, op)
		_, err := conn.Write(append(h, b...))
		return err
	}
	for {
		var h [6]byte
		if _, err := io.ReadFull(conn, h[:]); err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		n := int(binary.BigEndian.Uint16(h[:2])) - 4
		if n < 0 {
			return fmt.Errorf("bad client packet size")
		}
		b := make([]byte, n)
		if _, err := io.ReadFull(conn, b); err != nil {
			return err
		}
		switch op := binary.LittleEndian.Uint32(h[2:]); op {
		case opcode.CMSGPlayerLogin:
			if err := write(opcode.SMSGLoginVerifyWorld, make([]byte, 20)); err != nil {
				return err
			}
			if err := write(opcode.SMSGTimeSyncReq, []byte{1, 0, 0, 0}); err != nil {
				return err
			}
		case opcode.CMSGTimeSyncResp:
		case opcode.CMSGQueryTime:
			sent <- b
		case opcode.CMSGPing:
			if err := write(opcode.SMSGPong, b[:4]); err != nil {
				return err
			}
		case opcode.CMSGLogoutRequest:
			if err := write(opcode.SMSGLogoutComplete, nil); err != nil {
				return err
			}
		default:
			return fmt.Errorf("unexpected client opcode %x", op)
		}
	}
}
