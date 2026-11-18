// Package modulefixture implements non-gameplay worker/orchestrator fixtures.
// It is used by tests and the separately built fixture image, never agent-wow.
package modulefixture

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	fixturev1 "github.com/agent-wow/agent-wow/internal/modulefixture/api"
	"github.com/agent-wow/agent-wow/pkg/modules/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/emptypb"
)

type Service struct {
	fixturev1.UnimplementedFixtureServer
	Session           modv1.SessionClient
	Dependency        string
	PacketReplyOpcode uint32
	Call              func(context.Context, *fixturev1.Request) error
	Before            func(context.Context) error
	Packet            func(context.Context, *modv1.WorldPacket) error
	Packets           atomic.Uint32
	mu                sync.Mutex
	background        context.CancelFunc
	finished          chan struct{}
	preparing         bool
	nextCall          uint64
	active            map[uint64]context.CancelFunc
	calls             sync.WaitGroup
}

func (s *Service) Execute(ctx context.Context, req *fixturev1.Request) (*fixturev1.Result, error) {
	s.mu.Lock()
	if s.preparing {
		s.mu.Unlock()
		return nil, status.Error(codes.FailedPrecondition, "preparing logout")
	}
	ctx, cancel := context.WithCancel(ctx)
	s.nextCall++
	id := s.nextCall
	if s.active == nil {
		s.active = map[uint64]context.CancelFunc{}
	}
	s.active[id] = cancel
	s.calls.Add(1)
	s.mu.Unlock()
	defer func() {
		cancel()
		s.mu.Lock()
		delete(s.active, id)
		s.mu.Unlock()
		s.calls.Done()
	}()
	if s.Call != nil {
		if err := s.Call(ctx, req); err != nil {
			return nil, err
		}
	}
	if req.DelayMs > 0 {
		timer := time.NewTimer(time.Duration(req.DelayMs) * time.Millisecond)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		case <-timer.C:
		}
	}
	if req.Failure != "" {
		code := codes.InvalidArgument
		if req.Failure == "unavailable" {
			code = codes.Unavailable
		}
		return nil, status.Error(code, req.Failure)
	}
	if s.Dependency != "" {
		input, err := anypb.New(req)
		if err != nil {
			return nil, err
		}
		result, err := s.Session.InvokeModule(ctx, &modv1.InvokeModuleRequest{Module: s.Dependency, Method: "execute", Request: input})
		if err != nil {
			return nil, err
		}
		var response fixturev1.Result
		if err := result.Result.UnmarshalTo(&response); err != nil {
			return nil, err
		}
		return &response, nil
	}
	clock, err := s.Session.GetClock(ctx, &emptypb.Empty{})
	if err != nil {
		return nil, err
	}
	if req.Opcode != 0 {
		if _, err := s.Session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: req.Opcode, Payload: req.Payload}); err != nil {
			return nil, err
		}
	}
	return &fixturev1.Result{Number: req.Number, Payload: req.Payload, ClientTimeMs: clock.ClientTimeMs, Packets: s.Packets.Load()}, nil
}
func (s *Service) OnPacket(ctx context.Context, packet *modv1.WorldPacket) (*emptypb.Empty, error) {
	s.Packets.Add(1)
	if s.Packet != nil {
		if err := s.Packet(ctx, packet); err != nil {
			return nil, err
		}
	}
	if s.PacketReplyOpcode != 0 {
		if _, err := s.Session.SendPacket(ctx, &modv1.SendPacketRequest{Opcode: s.PacketReplyOpcode, Payload: packet.Payload}); err != nil {
			return nil, err
		}
	}
	return &emptypb.Empty{}, nil
}
func (s *Service) BeforeLogout(ctx context.Context, _ *emptypb.Empty) (*emptypb.Empty, error) {
	s.mu.Lock()
	s.preparing = true
	for _, cancel := range s.active {
		cancel()
	}
	s.mu.Unlock()
	defer func() { s.mu.Lock(); s.preparing = false; s.mu.Unlock() }()
	s.calls.Wait()
	s.StopBackground()
	if s.Dependency != "" {
		input, _ := anypb.New(&fixturev1.Request{})
		if _, err := s.Session.InvokeModule(ctx, &modv1.InvokeModuleRequest{Module: s.Dependency, Method: "execute", Request: input}); err != nil {
			return nil, err
		}
	}
	if s.Before != nil {
		if err := s.Before(ctx); err != nil {
			return nil, err
		}
	}
	return &emptypb.Empty{}, nil
}

// StartBackground demonstrates autonomous callback use and cancellation. The
// fixture samples the shared clock, and stops if its session becomes unavailable.
func (s *Service) StartBackground(ctx context.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	s.background = cancel
	s.finished = make(chan struct{})
	go func() {
		defer close(s.finished)
		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				call, end := context.WithTimeout(ctx, time.Second)
				_, err := s.Session.GetClock(call, &emptypb.Empty{})
				end()
				if err != nil {
					return
				}
			}
		}
	}()
}
func (s *Service) StopBackground() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.background != nil {
		s.background()
		<-s.finished
		s.background = nil
	}
}
