package char

import (
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
)

func ptr[T any](v T) *T { return &v }

func TestEnums(t *testing.T) {
	for text, want := range map[string]Race{"human": RaceHuman, "NIGHT_ELF": RaceNightElf, "blood-elf": RaceBloodElf, "11": RaceDraenei} {
		if got, err := ParseRace(text); err != nil || got != want {
			t.Fatalf("%s: %v %v", text, got, err)
		}
	}
	if got, err := ParseClass("death knight"); err != nil || got != ClassDeathKnight {
		t.Fatal(got, err)
	}
	if got, err := ParseGender("0"); err != nil || got != GenderMale {
		t.Fatal(got, err)
	}
	for _, input := range []string{"", "9", "256", "-1", "goblin"} {
		if _, err := ParseRace(input); err == nil {
			t.Fatal("accepted", input)
		}
	}
	if Race(255).String() != "255" || ClassDruid.String() != "Druid" || GenderFemale.String() != "Female" {
		t.Fatal("incorrect enum display")
	}
}

func TestCatalogue(t *testing.T) {
	if catalogue.Build != 12340 || len(catalogue.RaceClasses) != 62 || len(catalogue.Profiles) != 40 {
		t.Fatal("invalid catalogue metadata")
	}
	seen := map[[3]uint8]bool{}
	for _, p := range catalogue.Profiles {
		dk := uint8(0)
		if p.DeathKnight {
			dk = 1
		}
		key := [3]uint8{uint8(p.Race), uint8(p.Gender), dk}
		if seen[key] || len(p.SkinFaces) == 0 || len(p.Hair) == 0 {
			t.Fatal("invalid appearance profile", key)
		}
		seen[key] = true
		if _, ok := raceNames[p.Race]; !ok {
			t.Fatal("nonplayer race", p.Race)
		}
		if p.Gender > GenderFemale {
			t.Fatal("nonplayer gender")
		}
		for i, sf := range p.SkinFaces {
			if i > 0 && slices.Compare(p.SkinFaces[i-1][:], sf[:]) >= 0 {
				t.Fatal("duplicate or unsorted skin/face")
			}
		}
		for i, h := range p.Hair {
			if i > 0 && slices.Compare(p.Hair[i-1][:], h[:]) >= 0 {
				t.Fatal("duplicate or unsorted hair")
			}
		}
	}
	// Independent stock-rules examples, including Wrath's Blood Elf restriction.
	for _, pair := range [][2]uint8{{1, 1}, {1, 2}, {4, 11}, {6, 11}, {11, 7}, {10, 2}} {
		if !slices.Contains(catalogue.RaceClasses, pair) {
			t.Fatal("missing race/class", pair)
		}
	}
	for _, pair := range [][2]uint8{{10, 1}, {1, 11}, {7, 2}, {2, 8}} {
		if slices.Contains(catalogue.RaceClasses, pair) {
			t.Fatal("invalid race/class", pair)
		}
	}
}

func TestResolvePartialOptions(t *testing.T) {
	rng := rand.New(rand.NewPCG(12, 34))
	c := &Client{expansion: 2, choose: rng.IntN}
	for _, options := range []CreateOptions{
		{}, {Name: ptr("Arlen")}, {Race: ptr(RaceHuman)}, {Class: ptr(ClassDruid)}, {Gender: ptr(GenderFemale)},
		{Skin: ptr(uint8(0))}, {Face: ptr(uint8(0))}, {HairStyle: ptr(uint8(0))}, {HairColor: ptr(uint8(0))}, {FacialHair: ptr(uint8(0))},
		{Skin: ptr(uint8(18))}, {Race: ptr(RaceHuman), Class: ptr(ClassWarrior), Gender: ptr(GenderMale), Skin: ptr(uint8(0)), Face: ptr(uint8(0)), HairStyle: ptr(uint8(0)), HairColor: ptr(uint8(0)), FacialHair: ptr(uint8(0))},
	} {
		for range 30 {
			got, err := c.resolve(options, nil)
			if err != nil {
				t.Fatalf("%s: %v", describeOptions(options), err)
			}
			assertResolved(t, options, got)
			if got.Class == ClassDeathKnight {
				t.Fatal("automatically chose locked Death Knight")
			}
		}
	}
}

func assertGeneratedName(t *testing.T, name string) {
	t.Helper()
	if len(name) != 12 || name[0] < 'A' || name[0] > 'Z' {
		t.Fatalf("expected a 12-letter name with an uppercase initial, got %q", name)
	}
	for _, ch := range name[1:] {
		if ch < 'a' || ch > 'z' {
			t.Fatalf("expected lowercase ASCII letters after the initial, got %q", name)
		}
	}
	lower := strings.ToLower(name)
	for ch := 'a'; ch <= 'z'; ch++ {
		if strings.Contains(lower, strings.Repeat(string(ch), 3)) {
			t.Fatalf("three identical consecutive letters in %q", name)
		}
	}
	if strings.HasSuffix(lower, "gm") {
		t.Fatalf("reserved suffix in %q", name)
	}
}

func TestGeneratedNames(t *testing.T) {
	c := &Client{choose: rand.New(rand.NewPCG(123, 456)).IntN}
	var seen [12][26]bool
	for range 1000 {
		name := c.generatedName()
		assertGeneratedName(t, name)
		for i, ch := range strings.ToLower(name) {
			seen[i][ch-'a'] = true
		}
	}
	// Every position must use the full alphabet, not a restricted syllable set.
	for i, letters := range seen {
		for ch, present := range letters {
			if !present {
				t.Errorf("letter %c was never generated at position %d", 'a'+ch, i)
			}
		}
	}
}

func TestGeneratedNameRejectsInvalidCandidates(t *testing.T) {
	for _, invalid := range []string{
		"aaabcdefghij", // Triple at the start, before capitalization.
		"abcdxxxhijkl", // Triple in the middle.
		"abcdefghijjj", // Triple at the end.
		"abcdefghijgm", // Reserved suffix.
	} {
		t.Run(invalid, func(t *testing.T) {
			// Double repeats are valid and must not be discarded.
			draws := invalid + "aabbccddeeff"
			index := 0
			c := &Client{choose: func(n int) int {
				if n != 26 || index >= len(draws) {
					t.Fatal("unexpected random draw", n, index)
				}
				value := int(draws[index] - 'a')
				index++
				return value
			}}
			name := c.generatedName()
			if name != "Aabbccddeeff" || index != len(draws) {
				t.Fatalf("invalid candidate was not replaced: %q (%d draws)", name, index)
			}
			assertGeneratedName(t, name)
		})
	}
}

func assertResolved(t *testing.T, o CreateOptions, got CreateResult) {
	t.Helper()
	a := got.Appearance
	if !matches(o.Name, got.Name) || !matches(o.Race, got.Race) || !matches(o.Class, got.Class) || !matches(o.Gender, got.Gender) || !matches(o.Skin, a.Skin) || !matches(o.Face, a.Face) || !matches(o.HairStyle, a.HairStyle) || !matches(o.HairColor, a.HairColor) || !matches(o.FacialHair, a.FacialHair) {
		t.Fatal("explicit option changed", got)
	}
	if !slices.Contains(catalogue.RaceClasses, [2]uint8{uint8(got.Race), uint8(got.Class)}) {
		t.Fatal("invalid pair", got)
	}
	valid := false
	for _, p := range catalogue.Profiles {
		if p.Race == got.Race && p.Gender == got.Gender && p.DeathKnight == (got.Class == ClassDeathKnight) {
			valid = slices.Contains(p.SkinFaces, [2]uint8{a.Skin, a.Face}) && slices.Contains(p.Hair, [3]uint8{a.HairStyle, a.HairColor, a.FacialHair})
		}
	}
	if !valid {
		t.Fatal("invalid appearance", got)
	}
	if o.Name == nil {
		assertGeneratedName(t, got.Name)
	}
}

func TestResolveConstraints(t *testing.T) {
	for _, o := range []CreateOptions{
		{Name: ptr("")}, {Name: ptr("x\x00y")}, {Name: ptr("abcdefghijklm")}, {Race: ptr(Race(9))}, {Class: ptr(Class(10))}, {Gender: ptr(Gender(2))},
		{Race: ptr(RaceHuman), Class: ptr(ClassDruid)}, {Skin: ptr(uint8(255))}, {HairStyle: ptr(uint8(255))}, {HairColor: ptr(uint8(255))}, {Face: ptr(uint8(255))}, {FacialHair: ptr(uint8(255))},
	} {
		if _, err := (&Client{expansion: 2}).resolve(o, nil); err == nil {
			t.Fatal("accepted incompatible options", describeOptions(o))
		}
	}
	if _, err := (&Client{}).resolve(CreateOptions{Race: ptr(RaceDraenei)}, nil); err == nil {
		t.Fatal("ignored account expansion")
	}
	c := &Client{expansion: 2, choose: func(n int) int { return n - 1 }}
	c.realm.Type = 1
	characters := []Character{{Race: RaceHuman, Level: 60}}
	for range 20 {
		got, err := c.resolve(CreateOptions{Name: ptr("Arlen")}, characters)
		if err != nil || faction(got.Race) != 1 {
			t.Fatal("PvP faction", got, err)
		}
	}
	// Explicit choices bypass conservative stock eligibility guesses. The server
	// still validates its actual configurable level, faction and class rules.
	got, err := c.resolve(CreateOptions{Name: ptr("Arlen"), Race: ptr(RaceOrc), Class: ptr(ClassDeathKnight)}, nil)
	if err != nil || got.Race != RaceOrc || got.Class != ClassDeathKnight {
		t.Fatal(got, err)
	}
	for _, tc := range []struct {
		chars    []Character
		flag     uint32
		wantHero bool
	}{
		{nil, 0, false}, {nil, 0x20000000, true}, {[]Character{{Level: 55}}, 0, true}, {[]Character{{Level: 80, Class: ClassDeathKnight}}, 0x20000000, false},
	} {
		c.accountFlags = tc.flag
		// Skin 14 is a human Death Knight appearance, so a single
		// explicit appearance constraint also tests backwards filtering.
		_, err := c.resolve(CreateOptions{Name: ptr("Arlen"), Race: ptr(RaceHuman), Skin: ptr(uint8(14))}, tc.chars)
		if (err == nil) != tc.wantHero {
			t.Fatalf("hero eligibility: %+v: %v", tc, err)
		}
	}
	_, err = c.resolve(CreateOptions{Race: ptr(RaceHuman), Class: ptr(ClassDruid)}, nil)
	if !strings.Contains(err.Error(), "race=Human") || !strings.Contains(err.Error(), "class=Druid") {
		t.Fatal("missing conflict detail", err)
	}
}
