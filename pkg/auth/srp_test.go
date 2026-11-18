package auth

import (
	"encoding/hex"
	"math/big"
	"testing"
)

// Independent Python hashlib/pow fixture using the SERVER equation
// S = (A * v^u)^b mod N, with salt=00..1f, a=20..3f and b=40..5f.
// These pin wire byte order, both proofs and the 40-byte session key.
func TestSRPServerVector(t *testing.T) {
	c := testChallenge(t)
	a := fromLittleEndian(decodeHex(t, "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f"))
	p, err := calculateProof("PLAYER", "PASSWORD", c, a)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		got  []byte
		want string
	}{
		{"A", p.public[:], "6f3028cbf2bcc9774d0bb40aaefc9b272df4a5bc763813934e9965915a842c73"},
		{"K", p.key[:], "cf6fa19c6241d4294a0831aa7e496b7f4d3992edf7043c39f671640ecbb79797f415ce1d5551fea2"},
		{"M1", p.client[:], "de71d727df0fdc8b056b6b6db25e97cde0418dd8"},
		{"M2", p.server[:], "5e6c204da04fa1d4da0cce756ea1893a4937abe4"},
	} {
		if hex.EncodeToString(tc.got) != tc.want {
			t.Errorf("%s = %x, want %s", tc.name, tc.got, tc.want)
		}
	}
}

func TestSessionKeyLeadingZeros(t *testing.T) {
	for _, tc := range []struct {
		zeros int
		want  string
	}{
		{0, "ed2976c475109d044df3443c9cc86c31077e9c7db8e04d3d699f68bd6bcc6c6b20df81f6b7d66fa0"},
		{1, "9f32f1e4f14df7375d78e0a1d04857bada15d3d91e3ba8100472b55f54c99c703e7592b8e38cb19c"},
		{2, "3f9f6af149f11ff7115d70e059d00e57eddabed33a1e64a8f604e7b58c542b9c663ef09221e3d8b1"},
		{3, "6d32c89c1f54e5af2dcec0685b56e4ccc278cc01cda33596745a2aef74980445fc11eed83abbac49"},
		{31, "dada3939a3a3eeee5e5e6b6b4b4b0d0d32325555bfbfefef9595606018189090afafd8d807070909"},
		{32, "dada3939a3a3eeee5e5e6b6b4b4b0d0d32325555bfbfefef9595606018189090afafd8d807070909"},
	} {
		var shared [32]byte
		for i := tc.zeros; i < len(shared); i++ {
			shared[i] = byte(i - tc.zeros + 1)
		}
		key := sessionKey(shared)
		if hex.EncodeToString(key[:]) != tc.want {
			t.Errorf("%d leading zeros: got %x, want %s", tc.zeros, key, tc.want)
		}
	}
}

func TestRejectInvalidPublicKey(t *testing.T) {
	for _, public := range [][32]byte{{}, toLittleEndian(modulus)} {
		c := testChallenge(t)
		c.public = public
		if _, err := calculateProof("PLAYER", "PASSWORD", c, big.NewInt(1)); err == nil {
			t.Fatal("accepted invalid server public key")
		}
	}
}

func TestUpperLatin(t *testing.T) {
	if got := upperLatin("aZéßя 1"); got != "AZéßя 1" {
		t.Fatalf("normalization = %q", got)
	}
}

func testChallenge(t *testing.T) challenge {
	t.Helper()
	var c challenge
	copy(c.public[:], decodeHex(t, "582f0c854248e541e9cef4637f39e6b3897c5aa69471bd7e7e551a5b79d67318"))
	for i := range c.salt {
		c.salt[i] = byte(i)
	}
	return c
}

func decodeHex(t *testing.T, text string) []byte {
	t.Helper()
	data, err := hex.DecodeString(text)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
