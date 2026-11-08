// Package questsnapshot collects rewarded quest IDs over the public game protocol.
package questsnapshot

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
	"github.com/agent-wow/agent-wow/pkg/modules/runtime"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	"github.com/agent-wow/agent-wow/pkg/world"
)

// Collect briefly enters each owned character, queries its rewarded quests, and
// logs out before connecting the next one. It never loads workspace modules.
func Collect(ctx context.Context, realm auth.Realm, saved *auth.Session, characters []char.Character, timeout time.Duration) (map[char.GUID][]uint32, error) {
	if timeout <= 0 {
		return nil, errors.New("quest snapshot timeout must be greater than zero")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	result := make(map[char.GUID][]uint32, len(characters))
	for _, character := range characters {
		quests, err := collectCharacter(ctx, realm, saved, character.GUID, timeout)
		if err != nil {
			return nil, fmt.Errorf("collect completed quests for %s (GUID %d): %w", character.Name, character.GUID, err)
		}
		result[character.GUID] = quests
	}
	return result, nil
}

func collectCharacter(ctx context.Context, realm auth.Realm, saved *auth.Session, guid char.GUID, timeout time.Duration) (quests []uint32, err error) {
	entryCtx, cancelEntry := context.WithTimeout(ctx, timeout)
	defer cancelEntry()
	client, err := char.Dial(entryCtx, realm, saved)
	if err != nil {
		return nil, err
	}
	defer client.Close()
	observer := &questRuntime{Runtime: modrt.New(nil, nil, nil), responses: make(chan []byte, 1)}
	session, err := client.EnterWorld(entryCtx, guid, nil, world.Options{Modules: observer})
	cancelEntry()
	if err != nil {
		return nil, err
	}
	defer func() {
		// Give logout its own budget even when the query timed out. Closing the
		// socket alone can leave the character logged in on the server.
		logoutCtx, cancelLogout := context.WithTimeout(context.WithoutCancel(ctx), 31*time.Second)
		defer cancelLogout()
		if logoutErr := session.Logout(logoutCtx); logoutErr != nil {
			err = errors.Join(err, fmt.Errorf("log out observer: %w", logoutErr))
		}
		err = errors.Join(err, session.Close())
	}()
	queryCtx, cancelQuery := context.WithTimeout(ctx, timeout)
	defer cancelQuery()
	if err := session.SendPacket(queryCtx, opcode.CMSGQueryQuestsCompleted, nil); err != nil {
		return nil, fmt.Errorf("query completed quests: %w", err)
	}
	select {
	case body := <-observer.responses:
		return decodeCompleted(body)
	case <-queryCtx.Done():
		return nil, queryCtx.Err()
	case <-session.Done():
		if err := session.Err(); err != nil {
			return nil, err
		}
		return nil, errors.New("session closed before completed-quest response")
	}
}

// An empty runtime handles the session lifecycle; only the trusted observer
// consumes this response. No agent-authored parser or module runs here.
type questRuntime struct {
	modrt.Runtime
	responses chan []byte
}

func (r *questRuntime) Publish(op uint16, body []byte) error {
	if op != opcode.SMSGQueryQuestsCompletedResponse {
		return r.Runtime.Publish(op, body)
	}
	select {
	case r.responses <- append([]byte(nil), body...):
		return nil
	default:
		return errors.New("unexpected extra completed-quest response")
	}
}

func decodeCompleted(body []byte) ([]uint32, error) {
	if len(body) < 4 {
		return nil, errors.New("completed-quest response is missing its count")
	}
	count := binary.LittleEndian.Uint32(body[:4])
	if (len(body)-4)%4 != 0 || uint64(count) != uint64((len(body)-4)/4) {
		return nil, errors.New("completed-quest response length does not match its count")
	}
	quests := make([]uint32, 0, int(count))
	seen := make(map[uint32]bool, int(count))
	for offset := 4; offset < len(body); offset += 4 {
		id := binary.LittleEndian.Uint32(body[offset : offset+4])
		if id == 0 || seen[id] {
			return nil, fmt.Errorf("completed-quest response contains an invalid or duplicate quest ID: %d", id)
		}
		seen[id] = true
		quests = append(quests, id)
	}
	slices.Sort(quests)
	return quests, nil
}
