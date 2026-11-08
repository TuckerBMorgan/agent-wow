package cmd

import (
	"errors"
	"fmt"
	"text/tabwriter"
	"time"

	"github.com/agent-wow/agent-wow/internal/output"
	"github.com/agent-wow/agent-wow/internal/questsnapshot"
	"github.com/agent-wow/agent-wow/pkg/maps"
	"github.com/spf13/cobra"
)

func (deps characterCommands) listCommand(timeout *time.Duration) *cobra.Command {
	var jsonOutput, completedQuests bool
	command := &cobra.Command{Use: "list", Short: "List characters on the selected realm", Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if completedQuests && !jsonOutput {
				return errors.New("--completed-quests requires --json")
			}
			ctx, cancel, err := characterContext(cmd, *timeout)
			if err != nil {
				return err
			}
			defer cancel()
			realm, session, err := characterTarget(ctx)
			if err != nil {
				return err
			}
			client, err := deps.dial(ctx, realm, session)
			if err != nil {
				return err
			}
			characters, err := client.List(ctx)
			closeErr := client.Close()
			if err != nil {
				return err
			}
			if completedQuests {
				if closeErr != nil {
					return closeErr
				}
				cancel()
				quests, err := questsnapshot.Collect(cmd.Context(), realm, session, characters, *timeout)
				if err != nil {
					return err
				}
				return output.WriteCharacterJSONWithQuests(cmd.OutOrStdout(), realm, characters, quests)
			}
			if jsonOutput {
				return output.WriteCharacterJSON(cmd.OutOrStdout(), realm, characters)
			}
			out := cmd.OutOrStdout()
			if _, err := fmt.Fprintf(out, "Realm: %s (ID: %d)\n", realm.Name, realm.ID); err != nil {
				return err
			}
			if len(characters) == 0 {
				_, err = fmt.Fprintln(out, "No characters available.")
				return err
			}
			table := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
			fmt.Fprintln(table, "GUID\tNAME\tRACE\tCLASS\tGENDER\tLEVEL\tZONE\tMAP")
			for _, ch := range characters {
				fmt.Fprintf(table, "%d\t%s\t%s\t%s\t%s\t%d\t%s\t%s\n", ch.GUID, ch.Name, ch.Race, ch.Class, ch.Gender, ch.Level, maps.ZoneName(ch.ZoneID), maps.MapName(ch.MapID))
			}
			return table.Flush()
		}}
	command.Flags().BoolVar(&jsonOutput, "json", false, "Output characters and realm identity as JSON")
	command.Flags().BoolVar(&completedQuests, "completed-quests", false, "Log in each character to include rewarded quest IDs in JSON")
	return command
}
