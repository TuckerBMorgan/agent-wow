// Package config provides the client's storage, logging, and server settings.
package config

import (
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
)

// Server contains the host and TCP port of a server.
type Server struct {
	Host string `mapstructure:"host"`
	Port int    `mapstructure:"port"`
}

// Config contains the settings shared by client packages.
type Config struct {
	ConfigDir          string     `mapstructure:"config_dir"`
	DataDir            string     `mapstructure:"data_dir"`
	ModuleDir          string     `mapstructure:"module_dir"`
	AuthFilePath       string     `mapstructure:"auth_file_path"`
	RealmFilePath      string     `mapstructure:"realm_file_path"`
	AuthServer         Server     `mapstructure:"authserver"`
	WorldServerAddress string     `mapstructure:"worldserver_address"`
	WorldRPC           Server     `mapstructure:"worldrpc"`
	LogLevel           slog.Level `mapstructure:"log_level"`
}

var current Config

// Init loads defaults, a config file, and environment overrides. If configFile
// is empty, it looks for an optional file named config in the working directory
// (for example, config.yaml). An explicit configFile must exist. Environment
// variables use the AGENT_WOW prefix, such as AGENT_WOW_AUTHSERVER_HOST and
// AGENT_WOW_AUTHSERVER_PORT, AGENT_WOW_WORLDRPC_HOST, and
// AGENT_WOW_WORLDRPC_PORT. AGENT_WOW_LOG_LEVEL overrides the logging level.
// Environment variables take precedence over file settings.
// Call Init before starting any other client components.
func Init(configFile string) error {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("resolve home directory: %w", err)
	}
	v := viper.New()
	v.SetDefault("config_dir", filepath.Join(homeDir, ".config", "agent-wow"))
	v.SetDefault("data_dir", filepath.Join(homeDir, ".local", "share", "agent-wow"))
	v.SetDefault("authserver.host", "localhost")
	v.SetDefault("authserver.port", 3724)
	v.SetDefault("worldserver_address", "")
	v.SetDefault("worldrpc.host", "localhost")
	v.SetDefault("worldrpc.port", 8086)
	v.SetDefault("log_level", "info")

	if configFile != "" {
		v.SetConfigFile(configFile)
	} else {
		v.SetConfigName("config")
		v.AddConfigPath(".")
	}
	v.SetEnvPrefix("AGENT_WOW")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	v.AutomaticEnv()

	if err := v.ReadInConfig(); err != nil {
		var notFound viper.ConfigFileNotFoundError
		if configFile != "" || !errors.As(err, &notFound) {
			return fmt.Errorf("read config: %w", err)
		}
	}

	// Resolve these defaults after loading overrides so they follow ConfigDir.
	v.SetDefault("module_dir", filepath.Join(v.GetString("config_dir"), "modules"))
	v.SetDefault("auth_file_path", filepath.Join(v.GetString("config_dir"), "auth.json"))
	v.SetDefault("realm_file_path", filepath.Join(v.GetString("config_dir"), "realm.json"))
	var cfg Config
	if err := v.Unmarshal(&cfg, func(decoder *mapstructure.DecoderConfig) {
		// Let slog.Level parse and validate named levels while preserving
		// Viper's standard decoding behavior for the other settings.
		decoder.DecodeHook = mapstructure.ComposeDecodeHookFunc(decoder.DecodeHook, mapstructure.TextUnmarshallerHookFunc())
	}); err != nil {
		return fmt.Errorf("decode config: %w", err)
	}
	if strings.TrimSpace(cfg.ConfigDir) == "" {
		return errors.New("config_dir must not be empty")
	}
	if strings.TrimSpace(cfg.DataDir) == "" {
		return errors.New("data_dir must not be empty")
	}
	if strings.TrimSpace(cfg.ModuleDir) == "" {
		return errors.New("module_dir must not be empty")
	}
	if strings.TrimSpace(cfg.AuthFilePath) == "" {
		return errors.New("auth_file_path must not be empty")
	}
	if strings.TrimSpace(cfg.RealmFilePath) == "" {
		return errors.New("realm_file_path must not be empty")
	}
	if err := validateServer("authserver", cfg.AuthServer); err != nil {
		return err
	}
	if err := validateServer("worldrpc", cfg.WorldRPC); err != nil {
		return err
	}
	if cfg.WorldServerAddress != "" {
		host, port, err := net.SplitHostPort(cfg.WorldServerAddress)
		if err != nil {
			return fmt.Errorf("worldserver_address must be host:port: %w", err)
		}
		number, err := strconv.Atoi(port)
		if err != nil || strings.TrimSpace(host) == "" || number < 1 || number > 65535 {
			return errors.New("worldserver_address must have a host and a port between 1 and 65535")
		}
	}
	current = cfg
	return nil
}

// Get returns a copy of the configuration loaded by Init.
func Get() Config {
	return current
}

func validateServer(name string, server Server) error {
	if strings.TrimSpace(server.Host) == "" {
		return fmt.Errorf("%s.host must not be empty", name)
	}
	if server.Port < 1 || server.Port > 65535 {
		return fmt.Errorf("%s.port must be between 1 and 65535", name)
	}
	return nil
}
