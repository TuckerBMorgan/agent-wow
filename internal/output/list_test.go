package output_test

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/agent-wow/agent-wow/internal/output"
	"github.com/agent-wow/agent-wow/pkg/auth"
	"github.com/agent-wow/agent-wow/pkg/char"
)

func TestCharacterJSONUnknownLocation(t *testing.T) {
	var out bytes.Buffer
	err := output.WriteCharacterJSON(&out, auth.Realm{ID: 7, Name: "Live Realm"}, []char.Character{{GUID: 42, ZoneID: 0, MapID: 999999}})
	if err != nil {
		t.Fatal(err)
	}
	// Check the public keys independently of the output record's struct tags.
	var result struct {
		Characters []map[string]any `json:"characters"`
	}
	if err := json.Unmarshal(out.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Characters) != 1 {
		t.Fatal(out.String())
	}
	character := result.Characters[0]
	for key, want := range map[string]any{
		"guid": "42", "zone_id": float64(0), "zone_name": "Unknown zone (0)",
		"map_id": float64(999999), "map_name": "Unknown map (999999)",
	} {
		if got := character[key]; got != want {
			t.Errorf("%s = %v, want %v", key, got, want)
		}
	}
}

func TestCharacterJSONCompletedQuests(t *testing.T) {
	characters := []char.Character{{GUID: 42}, {GUID: 99}}
	var out bytes.Buffer
	err := output.WriteCharacterJSONWithQuests(&out, auth.Realm{}, characters, map[char.GUID][]uint32{42: {54, 928}, 99: {}})
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Characters []struct {
			GUID              string   `json:"guid"`
			CompletedQuestIDs []uint32 `json:"completed_quest_ids"`
		} `json:"characters"`
	}
	if err := json.Unmarshal(out.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Characters) != 2 || result.Characters[0].GUID != "42" ||
		len(result.Characters[0].CompletedQuestIDs) != 2 || result.Characters[0].CompletedQuestIDs[0] != 54 ||
		result.Characters[1].CompletedQuestIDs == nil || len(result.Characters[1].CompletedQuestIDs) != 0 {
		t.Fatal(out.String())
	}
	for _, quests := range []map[char.GUID][]uint32{nil, {}, {42: {54}}, {42: {54}, 99: nil}} {
		out.Reset()
		if err := output.WriteCharacterJSONWithQuests(&out, auth.Realm{}, characters, quests); err == nil || out.Len() != 0 {
			t.Fatal("missing evidence must not produce JSON", out.String(), err)
		}
	}
	out.Reset()
	if err := output.WriteCharacterJSON(&out, auth.Realm{}, characters); err != nil {
		t.Fatal(err)
	}
	var standard map[string]json.RawMessage
	var entries []map[string]json.RawMessage
	if err := json.Unmarshal(out.Bytes(), &standard); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(standard["characters"], &entries); err != nil {
		t.Fatal(err)
	}
	if _, ok := entries[0]["completed_quest_ids"]; ok {
		t.Fatal("standard list must not imply quest evidence was collected", out.String())
	}
}
