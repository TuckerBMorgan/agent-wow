package modrunner

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/modules/internal/testutil"
)

// Compose config validates the actual merge/interpolation rules without using
// the daemon. This catches mistakes hidden by the injected runtime test runner.
func TestComposeOverrideWithCLI(t *testing.T) {
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("Docker CLI not installed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := exec.CommandContext(ctx, "docker", "compose", "version").Run(); err != nil {
		t.Skip("Docker Compose not installed")
	}
	dir := t.TempDir()
	sockets := filepath.Join(dir, "sockets $literal")
	if err := os.Mkdir(sockets, 0700); err != nil {
		t.Fatal(err)
	}
	m := moddisc.Manifest{Directory: dir}
	m.Compose.File = "compose.yaml"
	m.Compose.Service = "module"
	modtest.WriteFile(t, filepath.Join(dir, "compose.yaml"), []byte("services:\n  module:\n    image: fixture\n    restart: always\n    environment:\n      OWN_SETTING: retained\n      AGENT_WOW_MODULE_NAME: wrong\n"))
	l := Launch{Manifest: m, Project: "aw-test", Override: filepath.Join(dir, "override.json"), SocketDir: sockets, Environment: map[string]string{"AGENT_WOW_MODULE_NAME": "fixture", "AGENT_WOW_REALM_NAME": "realm $literal"}}
	if err := writeOverride(l); err != nil {
		t.Fatal(err)
	}
	out, err := exec.CommandContext(ctx, "docker", append(composeArgs(l), "config", "--format", "json")...).CombinedOutput()
	if err != nil {
		t.Fatalf("compose config: %v\n%s", err, out)
	}
	var model struct {
		Services map[string]struct {
			Restart     string
			Environment map[string]string
			Volumes     []struct{ Source, Target string }
		}
	}
	if err := json.Unmarshal(out, &model); err != nil {
		t.Fatal(err)
	}
	s := model.Services["module"]
	// The canonical config re-escapes literal dollars so its output can itself
	// be loaded as a Compose file without interpolating those values again.
	if s.Restart != "no" || s.Environment["OWN_SETTING"] != "retained" || s.Environment["AGENT_WOW_MODULE_NAME"] != "fixture" || s.Environment["AGENT_WOW_REALM_NAME"] != "realm $$literal" {
		t.Fatalf("bad override: %s", out)
	}
	if len(s.Volumes) != 1 || s.Volumes[0].Source != strings.ReplaceAll(sockets, "$", "$$") || s.Volumes[0].Target != "/run/agent-wow" {
		t.Fatalf("bad socket mount: %s", out)
	}
}
