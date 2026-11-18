package main

import (
	"encoding/binary"
	"strings"
	"testing"
)

// A small independent WDBC fixture with IDs deliberately out of order. Other
// fields point at a different string so a wrong label column is detectable.
func fixture(fields, labelField int) []byte {
	strings := []byte("\x00Directory\x00Eastern Kingdoms\x00Kalimdor\x00")
	data := make([]byte, 20+2*fields*4+len(strings))
	copy(data, "WDBC")
	for i, n := range []uint32{2, uint32(fields), uint32(fields * 4), uint32(len(strings))} {
		binary.LittleEndian.PutUint32(data[4+i*4:], n)
	}
	for row := range 2 {
		for field := range fields {
			binary.LittleEndian.PutUint32(data[20+(row*fields+field)*4:], 1)
		}
	}
	binary.LittleEndian.PutUint32(data[20:], 1)
	binary.LittleEndian.PutUint32(data[20+fields*4:], 0)
	binary.LittleEndian.PutUint32(data[20+labelField*4:], 28)
	binary.LittleEndian.PutUint32(data[20+(fields+labelField)*4:], 11)
	copy(data[20+2*fields*4:], strings)
	return data
}

func TestDecodeLabels(t *testing.T) {
	for _, layout := range [][2]int{{66, 5}, {36, 11}} {
		labels, err := decodeLabels(fixture(layout[0], layout[1]), uint32(layout[0]), uint32(layout[1]))
		if err != nil {
			t.Fatal(err)
		}
		if len(labels) != 2 || labels[0] != "Eastern Kingdoms" || labels[1] != "Kalimdor" {
			t.Fatalf("unexpected labels: %v", labels)
		}
	}
}

func TestDecodeLabelsMalformed(t *testing.T) {
	for _, tc := range []struct {
		name string
		edit func([]byte) []byte
		want string
	}{
		{"header", func(b []byte) []byte { return b[:19] }, "header"},
		{"magic", func(b []byte) []byte { b[0] = 'X'; return b }, "header"},
		{"fields", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[8:], 65); return b }, "layout"},
		{"record size", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[12:], 4); return b }, "layout"},
		{"count overflow", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[4:], 0xffffffff); return b }, "length"},
		{"truncated", func(b []byte) []byte { return b[:len(b)-1] }, "length"},
		{"trailing data", func(b []byte) []byte { return append(b, 0) }, "length"},
		{"offset", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[40:], 0xffffffff); return b }, "offset"},
		{"terminator", func(b []byte) []byte { b[len(b)-1] = 'x'; return b }, "unterminated"},
		{"empty", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[40:], 0); return b }, "nonempty"},
		{"invalid UTF-8", func(b []byte) []byte { b[len(b)-2] = 0xff; return b }, "label"},
		{"control", func(b []byte) []byte { b[len(b)-2] = '\n'; return b }, "control"},
		{"duplicate ID", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[20:], 0); return b }, "duplicate"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := decodeLabels(tc.edit(fixture(66, 5)), 66, 5)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want %q", err, tc.want)
			}
		})
	}
}
