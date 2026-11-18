package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

func TestDecodeDBC(t *testing.T) {
	data := []byte("WDBC")
	for _, field := range []uint32{2, 2, 8, 1, 10, 20, 30, 40} {
		data = binary.LittleEndian.AppendUint32(data, field)
	}
	data = append(data, 0) // String block.
	rows, err := decodeDBC(data, 2)
	if err != nil || !reflect.DeepEqual(rows, [][]uint32{{10, 20}, {30, 40}}) {
		t.Fatal(rows, err)
	}
	for i := 0; i < len(data); i++ {
		if _, err := decodeDBC(data[:i], 2); err == nil {
			t.Fatalf("accepted truncated DBC at byte %d", i)
		}
	}
	for _, mutate := range []func([]byte) []byte{
		func(b []byte) []byte { b[0] = 'X'; return b },
		func(b []byte) []byte { b[8] = 3; return b },
		func(b []byte) []byte { b[12] = 4; return b },
		func(b []byte) []byte { binary.LittleEndian.PutUint32(b[4:8], ^uint32(0)); return b },
		func(b []byte) []byte { binary.LittleEndian.PutUint32(b[16:20], ^uint32(0)); return b },
		func(b []byte) []byte { return append(b, 0) },
	} {
		if _, err := decodeDBC(mutate(bytes.Clone(data)), 2); err == nil {
			t.Fatal("accepted malformed DBC")
		}
	}
}

func TestRaceClassPairs(t *testing.T) {
	var sql strings.Builder
	// Reversed and duplicated source rows must yield a stable sorted catalogue.
	for race := 10; race >= 1; race-- {
		for class := 6; class >= 1; class-- {
			fmt.Fprintf(&sql, "(%d,%d,0,0,1,2,3,4),\n", race, class)
		}
	}
	sql.WriteString("(1,1,0,0,1,2,3,4);\n")
	pairs, err := parseRaceClasses([]byte(sql.String()))
	if err != nil || len(pairs) != 60 || pairs[0] != [2]uint32{1, 1} || pairs[59] != [2]uint32{10, 6} {
		t.Fatal(pairs, err)
	}
	for _, input := range []string{"", "(1,1,0),\n", sql.String() + "(256,1,0),\n", sql.String() + "(0,1,0),\n"} {
		if _, err := parseRaceClasses([]byte(input)); err == nil {
			t.Fatal("accepted incomplete or invalid race/class data")
		}
	}
}

func TestAppearanceDependencies(t *testing.T) {
	section := func(kind, flags, variation, color uint32) []uint32 {
		return []uint32{0, 1, 0, kind, 0, 0, 0, flags, variation, color}
	}
	sections := [][]uint32{
		section(0, 1, 0, 2),  // Playable skin 2.
		section(1, 1, 3, 2),  // Face 3 exists for skin 2.
		section(1, 1, 9, 8),  // Face without a playable skin must be excluded.
		section(0, 5, 0, 4),  // Death Knight skin.
		section(1, 5, 6, 4),  // Death Knight face.
		section(0, 2, 0, 5),  // Nonplayer skin must be excluded.
		section(1, 2, 7, 5),  // Nonplayer face.
		section(3, 17, 1, 7), // Playable hair style/color.
		section(3, 17, 1, 7), // Duplicate row.
		section(3, 1, 99, 7), // Missing hair geoset.
		section(3, 1, 1, 8),  // No compatible facial texture color.
		section(2, 1, 2, 7),  // Facial style 2 can use hair color 7.
	}
	hairGeo := [][]uint32{{0, 1, 0, 1, 0, 0}}
	facialGeo := [][]uint32{{1, 0, 2, 0, 0, 0, 0, 0}, {1, 0, 3, 0, 0, 0, 0, 0}}
	for _, dk := range []bool{false, true} {
		p, err := buildProfile(1, 0, dk, sections, hairGeo, facialGeo)
		if err != nil {
			t.Fatal(err)
		}
		wantSkins := [][2]uint32{{2, 3}}
		if dk {
			wantSkins = append(wantSkins, [2]uint32{4, 6})
		}
		if !reflect.DeepEqual(p.SkinFaces, wantSkins) || !reflect.DeepEqual(p.Hair, [][3]uint32{{1, 7, 2}}) {
			t.Fatal("invalid appearance combinations", p)
		}
	}
	// Some facial features (horns, piercings) only have geosets, no textures.
	withoutFacialTextures := sections[:len(sections)-1]
	p, err := buildProfile(1, 0, false, withoutFacialTextures, hairGeo, facialGeo)
	if err != nil || !reflect.DeepEqual(p.Hair, [][3]uint32{{1, 7, 2}, {1, 7, 3}, {1, 8, 2}, {1, 8, 3}}) {
		t.Fatal("lost untextured facial features", p, err)
	}
	if _, err := buildProfile(1, 1, false, sections, hairGeo, facialGeo); err == nil {
		t.Fatal("accepted missing gender profile")
	}
	sections = append(sections, section(0, 1, 0, 256), section(1, 1, 0, 256))
	if _, err := buildProfile(1, 0, false, sections, hairGeo, facialGeo); err == nil {
		t.Fatal("accepted appearance index larger than one byte")
	}
}
