package maps_test

import (
	"testing"

	"github.com/agent-wow/agent-wow/pkg/maps"
)

func TestMapName(t *testing.T) {
	for id, want := range map[uint32]string{
		0: "Eastern Kingdoms", 1: "Kalimdor", 530: "Outland", 571: "Northrend",
		533: "Naxxramas", 999999: "Unknown map (999999)",
		0xffffffff: "Unknown map (4294967295)",
	} {
		if got := maps.MapName(id); got != want {
			t.Errorf("MapName(%d) = %q, want %q", id, got, want)
		}
	}
}

func TestZoneName(t *testing.T) {
	for id, want := range map[uint32]string{
		12: "Elwynn Forest", 14: "Durotar", 9: "Northshire Valley",
		1537: "Ironforge", 3456: "Naxxramas", 3524: "Azuremyst Isle", 4395: "Dalaran",
		0: "Unknown zone (0)", 999999: "Unknown zone (999999)",
		0xffffffff: "Unknown zone (4294967295)",
	} {
		if got := maps.ZoneName(id); got != want {
			t.Errorf("ZoneName(%d) = %q, want %q", id, got, want)
		}
	}
}
