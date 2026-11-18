// Package modtest creates disposable module installations for tests.
package modtest

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func WriteModule(t *testing.T, root, name, requires string) string {
	t.Helper()
	dir := filepath.Join(root, name)
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("../../../internal/modulefixture/fixture.pb")
	if err != nil {
		t.Fatal(err)
	}
	WriteFile(t, filepath.Join(dir, "module.pb"), data)
	WriteFile(t, filepath.Join(dir, "compose.yaml"), []byte("services:\n  module:\n    image: fixture\n"))
	WriteFile(t, filepath.Join(dir, "module.yaml"), []byte(`api_version: 1
enabled: true
requires: [`+requires+`]
compose:
  file: compose.yaml
  service: module
grpc:
  descriptor_set: module.pb
rpc:
  execute: /agentwow.fixture.v1.Fixture/Execute
packets:
  SMSG_LOGIN_VERIFY_WORLD: /agentwow.fixture.v1.Fixture/OnPacket
  SMSG_TRIGGER_CINEMATIC: /agentwow.fixture.v1.Fixture/OnPacket
lifecycle:
  before_logout: /agentwow.fixture.v1.Fixture/BeforeLogout
`))
	return dir
}

func WriteFile(t *testing.T, path string, body []byte) {
	t.Helper()
	if err := os.WriteFile(path, body, 0644); err != nil {
		t.Fatal(err)
	}
}

func EditFile(t *testing.T, path, old, new string) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	WriteFile(t, path, []byte(strings.ReplaceAll(string(b), old, new)))
}
