package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/agent-wow/agent-wow/internal/modulefixture"
	fixturev1 "github.com/agent-wow/agent-wow/internal/modulefixture/api"
	"github.com/agent-wow/agent-wow/pkg/modules/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	path := os.Getenv("AGENT_WOW_MODULE_SOCKET")
	listener, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(path, 0666); err != nil {
		return err
	}
	conn, err := grpc.NewClient("unix://"+os.Getenv("AGENT_WOW_SESSION_SOCKET"), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry(), grpc.WithDefaultCallOptions(grpc.MaxRetryRPCBufferSize(0)))
	if err != nil {
		return err
	}
	defer conn.Close()
	service := &modulefixture.Service{Session: modv1.NewSessionClient(conn), Dependency: os.Getenv("FIXTURE_DEPENDENCY")}
	server := grpc.NewServer()
	fixturev1.RegisterFixtureServer(server, service)
	healthy := health.NewServer()
	grpc_health_v1.RegisterHealthServer(server, healthy)
	healthy.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	service.StartBackground(ctx)
	defer service.StopBackground()
	go func() { <-ctx.Done(); server.Stop() }()
	return server.Serve(listener)
}
