// A private, short-lived provisioning client; uses only the public game protocol.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
	modrt "github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/world"
)

type packet struct {
	op   uint16
	data []byte
}
type observer struct {
	modrt.Runtime
	packets chan packet
}

func (o *observer) Publish(op uint16, data []byte) error {
	select {
	case o.packets <- packet{op, append([]byte(nil), data...)}:
	default:
		return errors.New("provisioning packet buffer full")
	}
	return nil
}

type request struct {
	Action     string     `json:"action"`
	Username   string     `json:"username"`
	Password   string     `json:"password"`
	Name       string     `json:"name"`
	Race       char.Race  `json:"race_id"`
	Class      char.Class `json:"class_id"`
	Appearance struct {
		Gender      char.Gender `json:"gender"`
		Skin        uint8       `json:"skin"`
		Face        uint8       `json:"face"`
		HairStyle   uint8       `json:"hair_style"`
		HairColor   uint8       `json:"hair_color"`
		FacialStyle uint8       `json:"facial_style"`
	} `json:"appearance"`
	Command string `json:"command"`
	Opcode  uint32 `json:"opcode"`
	Payload string `json:"payload"`
	Spell   uint32 `json:"spell"`
	Item    uint32 `json:"item_id"`
}

func itemReputation(data []byte, item uint32) (uint32, uint32, error) {
	if len(data) < 16 || binary.LittleEndian.Uint32(data) != item {
		return 0, 0, errors.New("item query was rejected")
	}
	pos := 16
	for i := 0; i < 4; i++ {
		n := bytes.IndexByte(data[pos:], 0)
		if n < 0 {
			return 0, 0, errors.New("invalid item name")
		}
		pos += n + 1
	}
	if len(data)-pos < 72 {
		return 0, 0, errors.New("truncated item requirements")
	}
	return binary.LittleEndian.Uint32(data[pos+64:]), binary.LittleEndian.Uint32(data[pos+68:]), nil
}

func chatPacket(command string, race char.Race) []byte {
	language := uint32(7) // Common; clients may not send universal-language chat.
	switch race {
	case char.RaceOrc, char.RaceUndead, char.RaceTauren, char.RaceTroll, char.RaceBloodElf:
		language = 1 // Orcish.
	}
	data := binary.LittleEndian.AppendUint32(nil, 1) // CHAT_MSG_SAY.
	data = binary.LittleEndian.AppendUint32(data, language)
	return append(append(data, []byte("."+command)...), 0)
}

func spellID(data []byte) uint32 {
	pos := 0
	for range 2 {
		if pos >= len(data) {
			return 0
		}
		mask := data[pos]
		pos++
		for i := 0; i < 8; i++ {
			if mask&(1<<i) != 0 {
				pos++
			}
		}
	}
	if pos+5 > len(data) {
		return 0
	}
	return binary.LittleEndian.Uint32(data[pos+1:])
}

func (o *observer) awaitPacket(ctx context.Context, op uint16, spell uint32) ([]byte, error) {
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case p := <-o.packets:
			if p.op == 0x130 && len(p.data) >= 6 {
				return nil, fmt.Errorf("spell %d rejected (code %d)", binary.LittleEndian.Uint32(p.data[1:]), p.data[5])
			}
			if p.op == 0x112 && len(p.data) > 0 && p.data[0] != 0 {
				return nil, fmt.Errorf("inventory operation rejected (code %d)", p.data[0])
			}
			if p.op == op && (op != 0x96 || bytes.Contains(p.data, []byte("Connected players:"))) && (spell == 0 || spellID(p.data) == spell) {
				return p.data, nil
			}
		}
	}
}

func run() error {
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 4096), 1<<20)
	out := json.NewEncoder(os.Stdout)
	if !in.Scan() {
		return errors.New("missing provisioning request")
	}
	var first request
	if err := json.Unmarshal(in.Bytes(), &first); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	login, err := auth.NewClient("authserver:3724")
	if err != nil {
		return err
	}
	saved, err := login.Login(ctx, first.Username, first.Password)
	if err != nil {
		return err
	}
	realms, err := login.ListRealms(ctx, saved)
	if err != nil {
		return err
	}
	if len(realms) != 1 {
		return errors.New("expected one local realm")
	}
	realm := realms[0]
	realm.Address = "worldserver:8085"
	c, err := char.Dial(ctx, realm, saved)
	if err != nil {
		return err
	}
	defer c.Close()
	characters, err := c.List(ctx)
	if err != nil {
		return err
	}
	var selected char.Character
	for _, ch := range characters {
		if ch.Name == first.Name {
			selected = ch
		}
	}
	if selected.GUID == 0 {
		if first.Action != "create" {
			return errors.New("unfinished character was not found on the staging account")
		}
		a := first.Appearance
		_, err = c.Create(ctx, char.CreateOptions{Name: &first.Name, Race: &first.Race, Class: &first.Class, Gender: &a.Gender, Skin: &a.Skin, Face: &a.Face, HairStyle: &a.HairStyle, HairColor: &a.HairColor, FacialHair: &a.FacialStyle})
		if err != nil {
			return err
		}
		// Creation enumerated twice already; the native server permits three per second.
		time.Sleep(time.Second)
		characters, err = c.List(ctx)
		if err != nil {
			return err
		}
		for _, ch := range characters {
			if ch.Name == first.Name {
				selected = ch
			}
		}
	}
	if selected.GUID == 0 || selected.Race != first.Race || selected.Class != first.Class {
		return errors.New("character creation did not match request")
	}
	if err = out.Encode(map[string]any{"ok": true, "name": selected.Name, "guid": uint64(selected.GUID)}); err != nil {
		return err
	}
	log := slog.New(slog.DiscardHandler)
	o := &observer{Runtime: modrt.New(nil, nil, log), packets: make(chan packet, 8192)}
	var session *world.Session
	defer func() {
		if session != nil {
			session.Close()
		}
	}()
	for in.Scan() {
		var r request
		if err = json.Unmarshal(in.Bytes(), &r); err != nil {
			return err
		}
		ctx, done := context.WithTimeout(context.Background(), 35*time.Second)
		result := map[string]any{"ok": true}
		if r.Action != "enter" && session == nil {
			done()
			return errors.New("no active session")
		}
		switch r.Action {
		case "enter":
			session, err = c.EnterWorld(ctx, selected.GUID, log, world.Options{Modules: o, EntryTimeout: 30 * time.Second})
		case "logout":
			err = session.Logout(ctx)
		case "item":
			data := binary.LittleEndian.AppendUint32(nil, r.Item)
			data = binary.LittleEndian.AppendUint64(data, 0)
			err = session.SendPacket(ctx, 0x56, data)
			if err == nil {
				var reply []byte
				reply, err = o.awaitPacket(ctx, 0x58, 0)
				if err == nil {
					var faction, rank uint32
					faction, rank, err = itemReputation(reply, r.Item)
					result["reputation_faction"], result["reputation_rank"] = faction, rank
				}
			}
		case "chat", "packet":
			// Discard login chatter before sending a new, ordered request.
			draining := true
			for draining {
				select {
				case <-o.packets:
				default:
					draining = false
				}
			}
			op := r.Opcode
			var data []byte
			if r.Action == "chat" {
				op = 0x95
				data = chatPacket(r.Command, first.Race)
			} else {
				data, err = hex.DecodeString(r.Payload)
			}
			if err == nil {
				err = session.SendPacket(ctx, op, data)
			}
			if err == nil && r.Spell != 0 {
				_, err = o.awaitPacket(ctx, 0x132, r.Spell)
			}
			if err == nil && r.Action == "chat" {
				// Chat commands run on the world thread; a time query could overtake them.
				marker := chatPacket("server info", first.Race)
				err = session.SendPacket(ctx, 0x95, marker)
				if err == nil {
					_, err = o.awaitPacket(ctx, 0x96, 0)
				}
			}
			if err == nil {
				err = session.SendPacket(ctx, 0x1ce, nil)
			}
			if err == nil {
				_, err = o.awaitPacket(ctx, 0x1cf, 0)
			}
		default:
			err = errors.New("unknown provisioning action")
		}
		done()
		if err != nil {
			return err
		}
		if err = out.Encode(result); err != nil {
			return err
		}
		if r.Action == "logout" {
			return nil
		}
	}
	return in.Err()
}

// Keep the embedded runtime's lifecycle implementation and override only packet delivery.
var _ modrt.Runtime = (*observer)(nil)

func main() {
	if err := run(); err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": false, "error": err.Error()})
		os.Exit(1)
	}
}
