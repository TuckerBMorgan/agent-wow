package questsnapshot

import (
	"context"
	"encoding/binary"
	"errors"
	"slices"
	"testing"

	"github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/opcode"
)

func completedPacket(ids ...uint32) []byte {
	body := binary.LittleEndian.AppendUint32(nil, uint32(len(ids)))
	for _, id := range ids {
		body = binary.LittleEndian.AppendUint32(body, id)
	}
	return body
}

func TestDecodeCompleted(t *testing.T) {
	quests, err := decodeCompleted(completedPacket(928, 54, 8350))
	if err != nil || !slices.Equal(quests, []uint32{54, 928, 8350}) {
		t.Fatal(quests, err)
	}
	quests, err = decodeCompleted(completedPacket())
	if err != nil || quests == nil || len(quests) != 0 {
		t.Fatal("empty evidence must be a non-nil empty list", quests, err)
	}
	for name, body := range map[string][]byte{
		"missing count": nil,
		"short count":   {1, 0, 0},
		"missing quest": binary.LittleEndian.AppendUint32(nil, 1),
		"huge count":    binary.LittleEndian.AppendUint32(nil, 0xFFFFFFFF),
		"extra quest":   append(completedPacket(), 54, 0, 0, 0),
		"trailing byte": append(completedPacket(54), 0),
		"zero ID":       completedPacket(0),
		"duplicate ID":  completedPacket(54, 54),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeCompleted(body); err == nil {
				t.Fatal("accepted malformed response", body)
			}
		})
	}
}

func TestQuestRuntimeCopiesResponse(t *testing.T) {
	observer := &questRuntime{Runtime: modrt.New(nil, nil, nil), responses: make(chan []byte, 1)}
	defer observer.Close()
	if err := observer.Publish(opcode.SMSGLoginVerifyWorld, nil); err != nil {
		t.Fatal(err)
	}
	body := completedPacket(54)
	if err := observer.Publish(opcode.SMSGQueryQuestsCompletedResponse, body); err != nil {
		t.Fatal(err)
	}
	body[4] = 0
	quests, err := decodeCompleted(<-observer.responses)
	if err != nil || !slices.Equal(quests, []uint32{54}) {
		t.Fatal("response did not retain its own payload", quests, err)
	}
}

type fakeQuerySession struct {
	send func(context.Context, uint32, []byte) error
	done chan struct{}
	err  error
}

func (s *fakeQuerySession) SendPacket(ctx context.Context, op uint32, body []byte) error {
	return s.send(ctx, op, body)
}
func (s *fakeQuerySession) Done() <-chan struct{} { return s.done }
func (s *fakeQuerySession) Err() error           { return s.err }

func TestQueryCompleted(t *testing.T) {
	responses := make(chan []byte, 1)
	session := &fakeQuerySession{done: make(chan struct{}), send: func(ctx context.Context, op uint32, body []byte) error {
		if op != opcode.CMSGQueryQuestsCompleted || len(body) != 0 {
			t.Fatal("wrong public query", op, body)
		}
		responses <- completedPacket(54)
		return nil
	}}
	quests, err := queryCompleted(context.Background(), session, responses)
	if err != nil || !slices.Equal(quests, []uint32{54}) {
		t.Fatal(quests, err)
	}
}

func TestQueryFailuresDoNotBecomeEmptyEvidence(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, tc := range []struct {
		name string
		ctx  context.Context
		send error
		end  bool
	}{
		{name: "write failed", ctx: context.Background(), send: errors.New("write failed")},
		{name: "canceled", ctx: ctx},
		{name: "disconnected", ctx: context.Background(), end: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			done := make(chan struct{})
			if tc.end {
				close(done)
			}
			session := &fakeQuerySession{done: done, send: func(context.Context, uint32, []byte) error { return tc.send }}
			quests, err := queryCompleted(tc.ctx, session, make(chan []byte))
			if err == nil || quests != nil {
				t.Fatal("failure was treated as valid empty evidence", quests, err)
			}
		})
	}
}
