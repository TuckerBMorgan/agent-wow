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
