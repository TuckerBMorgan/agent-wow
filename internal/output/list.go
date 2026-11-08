// Package output formats command results for display.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"strconv"

	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
	"github.com/agent-wow/agent-wow/pkg/maps"
	realmtypes "github.com/agent-wow/agent-wow/pkg/realm"
)

func writeJSON(out io.Writer, value any) error {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

type realmListEntry struct {
	Selected   bool    `json:"selected"`
	ID         uint8   `json:"id"`
	Name       string  `json:"name"`
	Type       string  `json:"type"`
	Status     string  `json:"status"`
	Characters uint8   `json:"characters"`
	Population float32 `json:"population"`
	Address    string  `json:"address"`
}

// WriteRealmJSON writes the realm list envelope, including the current selection.
func WriteRealmJSON(out io.Writer, realms []auth.Realm, selectedID *uint8) error {
	entries := make([]realmListEntry, 0, len(realms))
	for _, r := range realms {
		entries = append(entries, realmListEntry{Selected: selectedID != nil && r.ID == *selectedID,
			ID: r.ID, Name: r.Name, Type: realmtypes.FormatType(r.Type), Status: r.Status(),
			Characters: r.Characters, Population: r.Population, Address: r.Address})
	}
	return writeJSON(out, struct {
		Realms []realmListEntry `json:"realms"`
	}{entries})
}

type characterListEntry struct {
	GUID              string   `json:"guid"`
	Name              string   `json:"name"`
	Race              string   `json:"race"`
	Class             string   `json:"class"`
	Gender            string   `json:"gender"`
	Level             uint8    `json:"level"`
	ZoneID            uint32   `json:"zone_id"`
	ZoneName          string   `json:"zone_name"`
	MapID             uint32   `json:"map_id"`
	MapName           string   `json:"map_name"`
	CompletedQuestIDs []uint32 `json:"completed_quest_ids,omitzero"`
}

// WriteCharacterJSON writes the character list and realm identity, using string
// GUIDs and map/zone labels alongside their numeric IDs.
func WriteCharacterJSON(out io.Writer, realm auth.Realm, characters []char.Character) error {
	return writeCharacterJSON(out, realm, characters, nil)
}

// WriteCharacterJSONWithQuests includes server-confirmed rewarded quests for
// every character. Missing snapshots are errors; empty snapshots encode as [].
func WriteCharacterJSONWithQuests(out io.Writer, realm auth.Realm, characters []char.Character, quests map[char.GUID][]uint32) error {
	if quests == nil {
		return fmt.Errorf("completed-quest evidence is missing")
	}
	return writeCharacterJSON(out, realm, characters, quests)
}

func writeCharacterJSON(out io.Writer, realm auth.Realm, characters []char.Character, quests map[char.GUID][]uint32) error {
	result := struct {
		Realm struct {
			ID   uint8  `json:"id"`
			Name string `json:"name"`
		} `json:"realm"`
		Characters []characterListEntry `json:"characters"`
	}{Characters: make([]characterListEntry, 0, len(characters))}
	result.Realm.ID, result.Realm.Name = realm.ID, realm.Name
	for _, ch := range characters {
		entry := characterListEntry{GUID: strconv.FormatUint(uint64(ch.GUID), 10),
			Name: ch.Name, Race: ch.Race.String(), Class: ch.Class.String(), Gender: ch.Gender.String(),
			Level: ch.Level, ZoneID: ch.ZoneID, ZoneName: maps.ZoneName(ch.ZoneID), MapID: ch.MapID, MapName: maps.MapName(ch.MapID)}
		if quests != nil {
			ids, ok := quests[ch.GUID]
			if !ok || ids == nil {
				return fmt.Errorf("completed-quest evidence is missing for character GUID %d", ch.GUID)
			}
			entry.CompletedQuestIDs = ids
		}
		result.Characters = append(result.Characters, entry)
	}
	return writeJSON(out, result)
}
