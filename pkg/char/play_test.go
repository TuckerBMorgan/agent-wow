package char

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/world"
)

func TestEnterWorldTransfersEncryptedConnection(t *testing.T) {
	saved := testSession()
	fixture := characterFixture(t)
	maintenance := make(chan struct{})
	maintained := make(chan struct{})
	realm := testRealm(t, func(p *realmPeer) error {
		if err := p.authenticate(saved, true); err != nil {
			return err
		}
		if _, err := p.expect(0x037); err != nil {
			return err
		}
		if err := p.write(0x03b, fixture); err != nil {
			return err
		}
		body, err := p.expect(0x03d)
		if err != nil || len(body) != 8 || binary.LittleEndian.Uint64(body) != 99 {
			return fmt.Errorf("login GUID: %x, %v", body, err)
		}
		position := binary.LittleEndian.AppendUint32(nil, 530)
		for _, f := range []float32{1.5, -2.5, 3.25, 1} {
			position = binary.LittleEndian.AppendUint32(position, math.Float32bits(f))
		}
		if err := p.write(0x236, position); err != nil {
			return err
		}
		if err := p.write(0x159, []byte{1, 99, 1}); err != nil {
			return err
		}
		// Client control is now opaque gameplay traffic; no core acknowledgement.
		if err := p.write(0x390, []byte{7, 0, 0, 0}); err != nil {
			return err
		}
		body, err = p.expect(0x391)
		if err != nil || len(body) != 8 || binary.LittleEndian.Uint32(body) != 7 {
			return fmt.Errorf("sync: %x, %v", body, err)
		}
		<-maintenance
		if err := p.write(0x390, []byte{8, 0, 0, 0}); err != nil {
			return err
		}
		body, err = p.expect(0x391)
		if err != nil || len(body) != 8 || binary.LittleEndian.Uint32(body) != 8 {
			return fmt.Errorf("second sync: %x, %v", body, err)
		}
		close(maintained)
		if body, err = p.expect(0x04b); err != nil || len(body) != 0 {
			return fmt.Errorf("logout: %x, %v", body, err)
		}
		if err := p.write(0x04c, []byte{0, 0, 0, 0, 1}); err != nil {
			return err
		}
		if err := p.write(0x04d, nil); err != nil {
			return err
		}
		_, _, err = p.read()
		if !errors.Is(err, io.EOF) {
			return fmt.Errorf("connection still open after logout: %v", err)
		}
		return nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	c, err := Dial(ctx, realm, saved)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug})).With("source", "injected")
	session, err := c.EnterWorld(ctx, 99, logger)
	if err != nil {
		close(maintenance)
		t.Fatal(err)
	}
	defer session.Close()
	cancel()
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	for _, operation := range []func() error{
		func() error { _, err := c.List(context.Background()); return err },
		func() error { _, err := c.Create(context.Background(), CreateOptions{}); return err },
		func() error { return c.Delete(context.Background(), 99) },
		func() error { _, err := c.EnterWorld(context.Background(), 99, nil); return err },
	} {
		if err := operation(); err == nil || !strings.Contains(err.Error(), "transferred") {
			t.Errorf("selection after handoff: %v", err)
		}
	}
	close(maintenance)
	select {
	case <-maintained:
	case <-time.After(time.Second):
		t.Fatal("startup cancellation/old Close killed gameplay")
	}
	state := session.Snapshot()
	if state.Status != world.InWorld || state.Character.Name != "Mira" || state.Realm.ID != 7 {
		t.Fatal(state)
	}
	if err := session.Logout(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-session.Done()
	if session.Err() != nil || session.Snapshot().Status != world.Closed {
		t.Fatal(session.Err())
	}
	for _, activity := range []string{
		"source=injected direction=write opcode=0x03d opcode_label=CMSG_PLAYER_LOGIN",
		"source=injected direction=read opcode=0x236 opcode_label=SMSG_LOGIN_VERIFY_WORLD",
		"source=injected direction=write opcode=0x04b opcode_label=CMSG_LOGOUT_REQUEST",
		"source=injected direction=read opcode=0x04d opcode_label=SMSG_LOGOUT_COMPLETE",
	} {
		if !strings.Contains(logs.String(), activity) {
			t.Errorf("injected logger missing %q in %s", activity, logs.String())
		}
	}
}

func TestEnterWorldOwnershipAndMalformedWarden(t *testing.T) {
	for _, integrity := range []bool{false, true} {
		t.Run(fmt.Sprint(integrity), func(t *testing.T) {
			saved := testSession()
			realm := testRealm(t, func(p *realmPeer) error {
				if err := p.authenticate(saved, false); err != nil {
					return err
				}
				if _, err := p.expect(0x037); err != nil {
					return err
				}
				if integrity {
					if err := p.write(0x2e6, []byte{1}); err != nil {
						return err
					}
				}
				if !integrity {
					if err := p.write(0x03b, characterFixture(t)); err != nil {
						return err
					}
				}
				_, _, err := p.read()
				if !errors.Is(err, io.EOF) {
					return fmt.Errorf("sent a login after ownership/protocol failure: %v", err)
				}
				return nil
			})
			c, err := Dial(context.Background(), realm, saved)
			if err != nil {
				t.Fatal(err)
			}
			defer c.Close()
			guid := GUID(12345)
			if integrity {
				guid = 99
			}
			_, err = c.EnterWorld(context.Background(), guid, nil)
			if err == nil {
				t.Fatal("entry should fail")
			}
			if integrity && !strings.Contains(err.Error(), "Warden") {
				t.Fatal(err)
			}
			if !integrity && !strings.Contains(err.Error(), "not in this account") {
				t.Fatal(err)
			}
		})
	}
}
