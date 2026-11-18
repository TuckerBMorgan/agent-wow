package clientdata

import (
	"errors"
	"os"
	"strings"
	"testing"
)

func TestMissingDataExplainsFetch(t *testing.T) {
	_, err := Read(t.TempDir(), "Map.dbc")
	if !errors.Is(err, os.ErrNotExist) || !strings.Contains(err.Error(), "./run client-data:fetch") {
		t.Fatalf("expected missing-data instructions, got %v", err)
	}
}
