package cmd

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agent-wow/agent-wow/internal/config"
)

func TestModuleListOffline(t *testing.T) {
	path := initTestConfig(t)
	root := t.TempDir()
	t.Setenv("AGENT_WOW_MODULE_DIR", root)
	if err := config.Init(path); err != nil {
		t.Fatal(err)
	}
	for _, asJSON := range []bool{false, true} {
		cmd := newModuleCommand()
		args := []string{"list"}
		if asJSON {
			args = append(args, "--json")
		}
		cmd.SetArgs(args)
		var out bytes.Buffer
		cmd.SetOut(&out)
		if err := cmd.Execute(); err != nil {
			t.Fatal(err)
		}
		if asJSON && !strings.Contains(out.String(), `"modules": []`) {
			t.Fatal(out.String())
		}
	}
	dir := filepath.Join(root, "example")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "module.yaml"), []byte("api_version: 1\ndescription: Offline fixture\nrequires: [worker]\nrpc:\n  run: /example.Service/Run\n"), 0644); err != nil {
		t.Fatal(err)
	}
	cmd := newModuleCommand()
	cmd.SetArgs([]string{"list", "--json"})
	var out bytes.Buffer
	cmd.SetOut(&out)
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	var result struct {
		Modules []struct {
			Name     string
			Enabled  bool
			Requires []string
			RPC      map[string]string
		}
	}
	if err := json.Unmarshal(out.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Modules) != 1 || result.Modules[0].Name != "example" || result.Modules[0].Enabled || result.Modules[0].RPC["run"] != "/example.Service/Run" {
		t.Fatal(out.String())
	}
}
