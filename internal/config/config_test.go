package config

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLogLevelConfig(t *testing.T) {
	for _, tc := range []struct {
		name, body, envLevel string
		want                 slog.Level
		wantErr              bool
	}{
		{name: "default info", body: "{}", want: slog.LevelInfo},
		{name: "debug in file", body: `{"log_level":"debug"}`, want: slog.LevelDebug},
		{name: "case insensitive", body: `{"log_level":"WARN"}`, want: slog.LevelWarn},
		{name: "environment overrides file", body: `{"log_level":"debug"}`, envLevel: "error", want: slog.LevelError},
		{name: "environment overrides default", body: "{}", envLevel: "debug", want: slog.LevelDebug},
		{name: "invalid file", body: `{"log_level":"verbose"}`, wantErr: true},
		{name: "empty file level", body: `{"log_level":""}`, wantErr: true},
		{name: "invalid environment", body: "{}", envLevel: "invalid", wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_LOG_LEVEL", tc.envLevel)
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.body), 0600); err != nil {
				t.Fatal(err)
			}
			err := Init(path)
			if tc.wantErr {
				if err == nil || !strings.Contains(err.Error(), "log_level") {
					t.Fatalf("expected log_level error, got %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := Get().LogLevel; got != tc.want {
				t.Fatalf("LogLevel=%s, want %s", got, tc.want)
			}
		})
	}
}

func TestWorldRPCConfig(t *testing.T) {
	for _, tc := range []struct {
		name, body, envHost, envPort string
		want                         Server
		wantErr                      string
	}{
		{name: "defaults", body: "{}", want: Server{Host: "localhost", Port: 8086}},
		{name: "file settings", body: `{"worldrpc":{"host":"127.0.0.1","port":9001}}`, want: Server{Host: "127.0.0.1", Port: 9001}},
		{name: "host only", body: `{"worldrpc":{"host":"::1"}}`, want: Server{Host: "::1", Port: 8086}},
		{name: "port only", body: `{"worldrpc":{"port":9002}}`, want: Server{Host: "localhost", Port: 9002}},
		{name: "environment overrides file", body: `{"worldrpc":{"host":"127.0.0.1","port":9001}}`, envHost: "::1", envPort: "9003", want: Server{Host: "::1", Port: 9003}},
		{name: "environment overrides defaults", body: "{}", envHost: "127.0.0.1", envPort: "9004", want: Server{Host: "127.0.0.1", Port: 9004}},
		{name: "empty host", body: `{"worldrpc":{"host":""}}`, wantErr: "worldrpc.host must not be empty"},
		{name: "zero port", body: `{"worldrpc":{"port":0}}`, wantErr: "worldrpc.port must be between 1 and 65535"},
		{name: "negative port", body: `{"worldrpc":{"port":-1}}`, wantErr: "worldrpc.port must be between 1 and 65535"},
		{name: "oversized port", body: `{"worldrpc":{"port":65536}}`, wantErr: "worldrpc.port must be between 1 and 65535"},
		{name: "invalid environment port", body: "{}", envPort: "65536", wantErr: "worldrpc.port must be between 1 and 65535"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_WORLDRPC_HOST", tc.envHost)
			t.Setenv("AGENT_WOW_WORLDRPC_PORT", tc.envPort)
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.body), 0600); err != nil {
				t.Fatal(err)
			}
			err := Init(path)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := Get().WorldRPC; got != tc.want {
				t.Fatalf("WorldRPC=%+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestStoragePaths(t *testing.T) {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	defaultConfigDir := filepath.Join(homeDir, ".config", "agent-wow")
	defaultDataDir := filepath.Join(homeDir, ".local", "share", "agent-wow")
	for _, tc := range []struct {
		name, config, envConfigDir, envDataDir, envFile, wantConfigDir, wantDataDir, wantFile, wantErr string
	}{
		{name: "defaults", config: `{}`, wantConfigDir: defaultConfigDir, wantDataDir: defaultDataDir, wantFile: filepath.Join(defaultConfigDir, "auth.json")},
		{name: "configured config directory", config: `{"config_dir":"/tmp/my-client"}`, wantConfigDir: "/tmp/my-client", wantDataDir: defaultDataDir, wantFile: "/tmp/my-client/auth.json"},
		{name: "separate data directory", config: `{"data_dir":"/tmp/db"}`, wantConfigDir: defaultConfigDir, wantDataDir: "/tmp/db", wantFile: filepath.Join(defaultConfigDir, "auth.json")},
		{name: "explicit auth file", config: `{"config_dir":"/tmp/my-client","auth_file_path":"/tmp/credentials.json"}`, wantConfigDir: "/tmp/my-client", wantDataDir: defaultDataDir, wantFile: "/tmp/credentials.json"},
		{name: "config directory environment override", config: `{"config_dir":"/tmp/file-dir"}`, envConfigDir: "/tmp/env-dir", wantConfigDir: "/tmp/env-dir", wantDataDir: defaultDataDir, wantFile: "/tmp/env-dir/auth.json"},
		{name: "data directory environment override", config: `{"data_dir":"/tmp/file-dir"}`, envDataDir: "/tmp/env-dir", wantConfigDir: defaultConfigDir, wantDataDir: "/tmp/env-dir", wantFile: filepath.Join(defaultConfigDir, "auth.json")},
		{name: "file environment override", config: `{"auth_file_path":"/tmp/from-config.json"}`, envFile: "/tmp/from-env.json", wantConfigDir: defaultConfigDir, wantDataDir: defaultDataDir, wantFile: "/tmp/from-env.json"},
		{name: "dev directories", config: `{"config_dir":"./config","data_dir":"./data"}`, wantConfigDir: "./config", wantDataDir: "./data", wantFile: "config/auth.json"},
		{name: "empty config directory", config: `{"config_dir":""}`, wantErr: "config_dir must not be empty"},
		{name: "empty data directory", config: `{"data_dir":""}`, wantErr: "data_dir must not be empty"},
		{name: "empty auth file", config: `{"auth_file_path":""}`, wantErr: "auth_file_path must not be empty"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_CONFIG_DIR", tc.envConfigDir)
			t.Setenv("AGENT_WOW_DATA_DIR", tc.envDataDir)
			t.Setenv("AGENT_WOW_MODULE_DIR", "")
			t.Setenv("AGENT_WOW_AUTH_FILE_PATH", tc.envFile)
			t.Setenv("AGENT_WOW_REALM_FILE_PATH", "")
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			err := Init(path)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			cfg := Get()
			if cfg.ConfigDir != tc.wantConfigDir || cfg.DataDir != tc.wantDataDir || cfg.AuthFilePath != tc.wantFile {
				t.Errorf("unexpected storage paths: ConfigDir=%q, DataDir=%q, AuthFilePath=%q", cfg.ConfigDir, cfg.DataDir, cfg.AuthFilePath)
			}
			if want := filepath.Join(tc.wantConfigDir, "realm.json"); cfg.RealmFilePath != want {
				t.Errorf("RealmFilePath=%q, want %q", cfg.RealmFilePath, want)
			}
			if want := filepath.Join(tc.wantConfigDir, "modules"); cfg.ModuleDir != want {
				t.Errorf("ModuleDir=%q, want %q", cfg.ModuleDir, want)
			}
		})
	}
}

func TestModuleDir(t *testing.T) {
	for _, tc := range []struct {
		name, config, envDir, want, wantErr string
	}{
		{name: "explicit directory", config: `{"module_dir":"/tmp/task-modules"}`, want: "/tmp/task-modules"},
		{name: "relative directory", config: `{"module_dir":"./task-modules"}`, want: "./task-modules"},
		{name: "environment overrides file", config: `{"module_dir":"/tmp/from-config"}`, envDir: "/tmp/from-env", want: "/tmp/from-env"},
		{name: "environment overrides default", config: `{}`, envDir: "/tmp/from-env", want: "/tmp/from-env"},
		{name: "empty directory", config: `{"module_dir":""}`, wantErr: "module_dir must not be empty"},
		{name: "whitespace directory", config: `{"module_dir":"  "}`, wantErr: "module_dir must not be empty"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_MODULE_DIR", tc.envDir)
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			err := Init(path)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := Get().ModuleDir; got != tc.want {
				t.Errorf("ModuleDir=%q, want %q", got, tc.want)
			}
		})
	}
}

func TestRealmFilePath(t *testing.T) {
	for _, tc := range []struct {
		name, config, envFile, want, wantErr string
	}{
		{name: "explicit file", config: `{"realm_file_path":"/tmp/selected-realm.json"}`, want: "/tmp/selected-realm.json"},
		{name: "environment override", config: `{"realm_file_path":"/tmp/from-config.json"}`, envFile: "/tmp/from-env.json", want: "/tmp/from-env.json"},
		{name: "empty file", config: `{"realm_file_path":""}`, wantErr: "realm_file_path must not be empty"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_WOW_REALM_FILE_PATH", tc.envFile)
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			err := Init(path)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := Get().RealmFilePath; got != tc.want {
				t.Errorf("RealmFilePath=%q, want %q", got, tc.want)
			}
		})
	}
}
