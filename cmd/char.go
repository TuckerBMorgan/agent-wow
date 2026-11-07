package cmd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/agent-wow/agent-wow/internal/config"
	"github.com/agent-wow/agent-wow/internal/questsnapshot"
	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
	"github.com/agent-wow/agent-wow/pkg/world"
	"github.com/spf13/cobra"
	"golang.org/x/term"
)

type characterClient interface {
	Close() error
	List(context.Context) ([]char.Character, error)
	Create(context.Context, char.CreateOptions) (char.CreateResult, error)
	Delete(context.Context, char.GUID) error
}

type characterCommands struct {
	dial            func(context.Context, auth.Realm, *auth.Session) (characterClient, error)
	play            func(context.Context, auth.Realm, *auth.Session, string, *slog.Logger, world.Options) (playSession, error)
	completedQuests func(context.Context, auth.Realm, *auth.Session, []char.Character, time.Duration) (map[char.GUID][]uint32, error)
	isTerminal      func(io.Reader) bool
}

func newCharCommand() *cobra.Command {
	return characterCommands{
		play:            startCharacterPlay,
		completedQuests: questsnapshot.Collect,
		dial: func(ctx context.Context, realm auth.Realm, session *auth.Session) (characterClient, error) {
			return char.Dial(ctx, realm, session)
		},
		isTerminal: func(input io.Reader) bool {
			file, ok := input.(interface{ Fd() uintptr })
			return ok && term.IsTerminal(int(file.Fd()))
		},
	}.command()
}

func (deps characterCommands) command() *cobra.Command {
	var timeout time.Duration
	command := &cobra.Command{Use: "char", Short: "List, create, delete and play characters on the selected realm", Args: cobra.NoArgs}
	command.PersistentFlags().DurationVar(&timeout, "timeout", 10*time.Second, "Timeout per network phase (excludes confirmation prompts)")
	command.AddCommand(deps.listCommand(&timeout), deps.createCommand(&timeout), deps.deleteCommand(&timeout), deps.playCommand(&timeout))
	return command
}

func characterTarget(ctx context.Context) (auth.Realm, *auth.Session, error) {
	selected, err := loadSelectedRealm()
	if err != nil {
		return auth.Realm{}, nil, err
	}
	if selected == nil {
		return auth.Realm{}, nil, errors.New("no realm selected; run 'agent-wow auth login' or 'agent-wow realm set <id|name>'")
	}
	session, err := loadSavedSession()
	if err != nil {
		return auth.Realm{}, nil, err
	}
	client, err := auth.NewClient(authServerAddress())
	if err != nil {
		return auth.Realm{}, nil, err
	}
	realms, err := client.ListRealms(ctx, session)
	if err != nil {
		return auth.Realm{}, nil, fmt.Errorf("fetch selected realm: %w; if the saved session is invalid, run 'agent-wow auth login'", err)
	}
	for _, realm := range realms {
		if realm.ID != selected.ID {
			continue
		}
		if !realm.Selectable() {
			return auth.Realm{}, nil, fmt.Errorf("selected realm %q is %s", realm.Name, realm.Status())
		}
		if address := config.Get().WorldServerAddress; address != "" {
			realm.Address = address
		}
		return realm, session, nil
	}
	return auth.Realm{}, nil, fmt.Errorf("selected realm %q is no longer listed; run 'agent-wow realm list' and 'agent-wow realm set <id|name>'", selected.Name)
}

func characterContext(cmd *cobra.Command, timeout time.Duration) (context.Context, context.CancelFunc, error) {
	cmd.SilenceUsage = true
	if timeout <= 0 {
		return nil, nil, errors.New("--timeout must be greater than zero")
	}
	ctx, cancel := context.WithTimeout(cmd.Context(), timeout)
	return ctx, cancel, nil
}

func findCharacter(characters []char.Character, selector string) (char.Character, error) {
	base := 10
	numeric := selector
	if strings.HasPrefix(strings.ToLower(numeric), "0x") {
		base, numeric = 16, numeric[2:]
	}
	if guid, err := strconv.ParseUint(numeric, base, 64); err == nil {
		for _, ch := range characters {
			if uint64(ch.GUID) == guid {
				return ch, nil
			}
		}
		return char.Character{}, fmt.Errorf("character GUID %q not found; run 'agent-wow char list'", selector)
	}
	var matches []char.Character
	for _, ch := range characters {
		if strings.EqualFold(ch.Name, selector) {
			matches = append(matches, ch)
		}
	}
	if len(matches) == 1 {
		return matches[0], nil
	}
	if len(matches) > 1 {
		return char.Character{}, fmt.Errorf("character name %q is ambiguous; use its GUID", selector)
	}
	return char.Character{}, fmt.Errorf("character %q not found; run 'agent-wow char list'", selector)
}

func init() { rootCmd.AddCommand(newCharCommand()) }
