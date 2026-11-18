package moddisc

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agent-wow/agent-wow/pkg/modules/internal/testutil"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/descriptorpb"
)

func TestDiscoveryAndGraph(t *testing.T) {
	root := t.TempDir()
	if got, err := Discover(filepath.Join(root, "missing")); err != nil || len(got) != 0 {
		t.Fatal(got, err)
	}
	modtest.WriteModule(t, root, "orchestrator", "worker")
	modtest.WriteModule(t, root, "worker", "")
	disabled := modtest.WriteModule(t, root, "disabled", "")
	modtest.EditFile(t, filepath.Join(disabled, "module.yaml"), "enabled: true", "enabled: false")
	os.Remove(filepath.Join(disabled, "module.pb"))
	items, err := Discover(root)
	if err != nil || len(items) != 3 {
		t.Fatal(items, err)
	}
	registry, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(registry.order, ",") != "worker,orchestrator" {
		t.Fatal(registry.order)
	}
	for _, tc := range []struct{ name, requires string }{{"missing", "absent"}, {"disabled", "disabled"}, {"self", "orchestrator"}, {"cycle", "worker"}} {
		t.Run(tc.name, func(t *testing.T) {
			base := t.TempDir()
			modtest.WriteModule(t, base, "orchestrator", tc.requires)
			dep := modtest.WriteModule(t, base, "worker", "")
			if tc.name == "cycle" {
				modtest.EditFile(t, filepath.Join(dep, "module.yaml"), "requires: []", "requires: [orchestrator]")
			}
			if _, err := Load(base); err == nil {
				t.Fatal("accepted invalid dependency graph")
			}
		})
	}
}

func TestManifestValidation(t *testing.T) {
	for _, tc := range []struct{ name, old, new string }{
		{"version", "api_version: 1", "api_version: 2"},
		{"unknown field", "enabled: true", "enabled: true\nunknown: true"},
		{"multiple documents", "enabled: true", "enabled: true\n---\nenabled: true"},
		{"duplicate YAML", "enabled: true", "enabled: true\nenabled: false"},
		{"unknown method", "/Fixture/Execute", "/Fixture/Missing"},
		{"unknown alias", "  execute:", "  bad.alias:"},
		{"client packet", "SMSG_LOGIN_VERIFY_WORLD", "CMSG_PLAYER_LOGIN"},
		{"bad packet signature", "Fixture/OnPacket", "Fixture/Execute"},
		{"bad hook signature", "Fixture/BeforeLogout", "Fixture/Execute"},
		{"missing compose service", "service: module", "service: other"},
		{"absolute descriptor", "descriptor_set: module.pb", "descriptor_set: /tmp/module.pb"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			dir := modtest.WriteModule(t, root, "worker", "")
			path := filepath.Join(dir, "module.yaml")
			old := tc.old
			if tc.name == "unknown method" {
				old = "Fixture/Execute"
			}
			modtest.EditFile(t, path, old, tc.new)
			if _, err := Load(root); err == nil {
				t.Fatal("accepted invalid manifest")
			}
		})
	}
	for _, name := range []string{"session", "bad.name"} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			modtest.WriteModule(t, root, name, "")
			if _, err := Discover(root); err == nil {
				t.Fatal("accepted reserved/invalid namespace")
			}
		})
	}
	t.Run("streaming", func(t *testing.T) {
		root := t.TempDir()
		dir := modtest.WriteModule(t, root, "worker", "")
		path := filepath.Join(dir, "module.pb")
		b, _ := os.ReadFile(path)
		var set descriptorpb.FileDescriptorSet
		if err := proto.Unmarshal(b, &set); err != nil {
			t.Fatal(err)
		}
		for _, f := range set.File {
			for _, s := range f.Service {
				if s.GetName() == "Fixture" {
					s.Method[0].ServerStreaming = proto.Bool(true)
				}
			}
		}
		b, _ = proto.Marshal(&set)
		modtest.WriteFile(t, path, b)
		if _, err := Load(root); err == nil || !strings.Contains(err.Error(), "unary") {
			t.Fatal(err)
		}
	})
	t.Run("numeric duplicate", func(t *testing.T) {
		root := t.TempDir()
		dir := modtest.WriteModule(t, root, "worker", "")
		modtest.EditFile(t, filepath.Join(dir, "module.yaml"), "SMSG_TRIGGER_CINEMATIC", "0x236")
		if _, err := Load(root); err == nil {
			t.Fatal("accepted duplicate subscription")
		}
	})
}
