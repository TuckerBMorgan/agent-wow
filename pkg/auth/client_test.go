package auth

import (
	"net"
	"testing"
	"time"
)

func TestNewClient(t *testing.T) {
	for _, address := range []string{"localhost:3724", "127.0.0.1:3724", "[::1]:3724"} {
		if client, err := NewClient(address); err != nil || client == nil {
			t.Fatalf("create client for %q: %v", address, err)
		}
	}
	for _, address := range []string{"", "localhost", ":3724", "localhost:", "localhost:abc", "localhost:0", "localhost:65536"} {
		if client, err := NewClient(address); err == nil || client != nil {
			t.Fatalf("accepted invalid authserver address %q", address)
		}
	}
}

func serve(t *testing.T, handler func(net.Conn) error) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
		done <- handler(conn)
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		if err := <-done; err != nil {
			t.Errorf("test authserver: %v", err)
		}
	})
	return listener.Addr().String()
}
