package moddisc_test

import (
	"strings"
	"testing"

	"github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/modules/internal/testutil"
)

func TestRegistryAccessorsProtectValidatedConfiguration(t *testing.T) {
	root := t.TempDir()
	modtest.WriteModule(t, root, "orchestrator", "worker")
	modtest.WriteModule(t, root, "worker", "")
	r, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	d, ok := r.Lookup("orchestrator")
	if !ok {
		t.Fatal("enabled module missing")
	}
	manifest := d.Manifest()
	manifest.Requires[0] = "undeclared"
	manifest.RPC["execute"] = "/invalid.Service/Method"
	delete(manifest.Packets, "SMSG_LOGIN_VERIFY_WORLD")
	order := r.Order()
	order[0] = "undeclared"

	if !d.Requires("worker") || d.Requires("undeclared") {
		t.Fatal("caller changed validated dependencies")
	}
	if got := strings.Join(r.Order(), ","); got != "worker,orchestrator" {
		t.Fatal("caller changed dependency order", got)
	}
	fresh := d.Manifest()
	md, ok := d.RPC("execute")
	if !ok || fresh.RPC["execute"] != md.Path() || fresh.Requires[0] != "worker" || len(fresh.Packets) != 2 {
		t.Fatal("caller changed validated manifest", fresh)
	}
	if _, ok := d.Packet(0x236); !ok {
		t.Fatal("caller changed validated subscription")
	}
}
