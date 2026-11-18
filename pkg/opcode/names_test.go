package opcode

import (
	"crypto/sha256"
	"fmt"
	"strings"
	"testing"
)

func TestWorldCatalogMatchesAzerothCore(t *testing.T) {
	// Independently calculated from Opcodes.h at revision
	// d80ce1d87720e6b6a0b9adc952f21a658b1c245e: one "0xNNN LABEL\n"
	// line per identifier, including NULL_OPCODE, excluding NUM_MSG_TYPES.
	const count = 0x521
	const wantSHA = "422302059ae18e42969c78a4df24ba8de7bd361709dfd3dfb51f6eee9090e0ce"
	if NumMsgTypes != count || len(worldNames) != count {
		t.Fatalf("catalog boundary/count = %d/%d, want %d", NumMsgTypes, len(worldNames), count)
	}
	var catalog strings.Builder
	for op := uint32(0); op < count; op++ {
		fmt.Fprintf(&catalog, "0x%03x %s\n", op, WorldName(op))
	}
	if got := fmt.Sprintf("%x", sha256.Sum256([]byte(catalog.String()))); got != wantSHA {
		t.Fatalf("world opcode values/labels differ from the pinned AzerothCore catalog: SHA-256 %s", got)
	}
}

func TestWorldNameOutsideCatalog(t *testing.T) {
	for _, op := range []uint32{0x521, 0xffff, 0x10000, 0xffffffff} {
		if got := WorldName(op); got != "UNKNOWN" {
			t.Errorf("WorldName(0x%x) = %q, want UNKNOWN", op, got)
		}
	}
}
