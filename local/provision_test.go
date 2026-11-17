package main

import (
	"context"
	"encoding/binary"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/char"
)

func TestChatUsesLearnedFactionLanguage(t *testing.T) {
	for _, test := range []struct {
		race     char.Race
		language uint32
	}{{char.RaceHuman, 7}, {char.RaceOrc, 1}} {
		data := chatPacket("server info", test.race)
		if binary.LittleEndian.Uint32(data[:4]) != 1 || binary.LittleEndian.Uint32(data[4:8]) != test.language || string(data[8:]) != ".server info\x00" {
			t.Fatalf("invalid native chat packet for race %d", test.race)
		}
	}
}

func TestItemQueryReputationRequirements(t *testing.T) {
	data := binary.LittleEndian.AppendUint32(nil, 50402)
	data = append(data, make([]byte, 12)...)
	data = append(data, []byte("Ashen ring\x00\x00\x00\x00")...)
	data = append(data, make([]byte, 64)...)
	data = binary.LittleEndian.AppendUint32(data, 1156)
	data = binary.LittleEndian.AppendUint32(data, 7)
	faction, rank, err := itemReputation(data, 50402)
	if err != nil || faction != 1156 || rank != 7 {
		t.Fatal(faction, rank, err)
	}
	if _, _, err := itemReputation(data[:len(data)-1], 50402); err == nil {
		t.Fatal("accepted truncated requirements")
	}
}

func TestServerSpellGoPacket(t *testing.T) {
	// Two packed GUIDs, cast count, spell ID, then cast flags.
	data := []byte{1, 7, 1, 9, 1}
	data = binary.LittleEndian.AppendUint32(data, 47898)
	data = append(data, 0, 0)
	if got := spellID(data); got != 47898 {
		t.Fatalf("spell ID: %d", got)
	}
	for n := 0; n < 9; n++ {
		if spellID(data[:n]) != 0 {
			t.Fatalf("accepted truncated packet at %d", n)
		}
	}
}

func TestWaitRejectsServerInventoryFailure(t *testing.T) {
	o := &observer{packets: make(chan packet, 2)}
	o.packets <- packet{0x112, []byte{48}}
	o.packets <- packet{0x1cf, nil}
	err := o.await(context.Background(), 0x1cf, 0)
	if err == nil || !strings.Contains(err.Error(), "inventory operation rejected") {
		t.Fatal(err)
	}
}

func TestWaitRequiresRequestedSpell(t *testing.T) {
	o := &observer{packets: make(chan packet, 1)}
	o.packets <- packet{0x132, []byte{0, 0, 1, 1, 0, 0, 0}}
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	if err := o.await(ctx, 0x132, 47898); err == nil {
		t.Fatal("accepted a different spell")
	}
}

func TestChatBarrierRequiresWorldThreadReply(t *testing.T) {
	o := &observer{packets: make(chan packet, 3)}
	o.packets <- packet{0x1cf, nil}
	o.packets <- packet{0x96, []byte("Other command output")}
	o.packets <- packet{0x96, []byte("Connected players: 1")}
	if err := o.await(context.Background(), 0x96, 0); err != nil {
		t.Fatal(err)
	}
	if len(o.packets) != 0 {
		t.Fatal("returned before the command completion marker")
	}
}
