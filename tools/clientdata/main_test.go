package main

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func testSource(name, content string) source {
	return source{name, fmt.Sprintf("%x", sha256.Sum256([]byte(content)))}
}

func TestDownloadAndReuse(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprint(w, "reference data")
	}))
	defer server.Close()
	dir := t.TempDir()
	src := testSource("data", "reference data")
	get := func() {
		t.Helper()
		if err := download(context.Background(), server.Client(), server.URL, dir, src, 100, io.Discard); err != nil {
			t.Fatal(err)
		}
	}
	get()
	get()
	if requests.Load() != 1 {
		t.Fatal("downloaded a verified cached file again")
	}
	// A corrupt local copy must be repaired on the next fetch.
	if err := os.WriteFile(filepath.Join(dir, src.name), []byte("corrupt"), 0o600); err != nil {
		t.Fatal(err)
	}
	get()
	if requests.Load() != 2 || !validFile(filepath.Join(dir, src.name), src.sha256) {
		t.Fatal("did not repair corrupt cache")
	}
	server.Close()
	get() // Complete cached downloads also work offline.
}

func TestDownloadFailurePreservesExistingFile(t *testing.T) {
	for _, tc := range []struct {
		name, want string
		serve      http.HandlerFunc
	}{
		{"HTTP error", "HTTP 502", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) }},
		{"wrong checksum", "checksum mismatch", func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "wrong") }},
		{"truncated", "unexpected EOF", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Length", "90")
			fmt.Fprint(w, "short")
		}},
		{"oversize header", "size limit", func(w http.ResponseWriter, r *http.Request) { w.Header().Set("Content-Length", "101") }},
		{"oversize stream", "size limit", func(w http.ResponseWriter, r *http.Request) {
			w.(http.Flusher).Flush()
			fmt.Fprint(w, strings.Repeat("x", 101))
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(tc.serve)
			defer server.Close()
			dir := t.TempDir()
			src := testSource("data", "new data")
			file := filepath.Join(dir, src.name)
			if err := os.WriteFile(file, []byte("old data"), 0o600); err != nil {
				t.Fatal(err)
			}
			err := download(context.Background(), server.Client(), server.URL, dir, src, 100, io.Discard)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want %q", err, tc.want)
			}
			data, err := os.ReadFile(file)
			if err != nil || string(data) != "old data" {
				t.Fatal("replaced existing data on failure", err)
			}
			entries, err := os.ReadDir(dir)
			if err != nil || len(entries) != 1 {
				t.Fatal("left a partial download", entries, err)
			}
		})
	}
}

func TestDownloadCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.(http.Flusher).Flush()
		cancel()
		<-r.Context().Done()
	}))
	defer server.Close()
	dir := t.TempDir()
	err := download(ctx, server.Client(), server.URL, dir, testSource("data", "content"), 100, io.Discard)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 0 {
		t.Fatal("cancelled download left files", entries, err)
	}
}

func TestExtract(t *testing.T) {
	for _, tc := range []struct {
		name, want string
		files      [][2]string
	}{
		{"nested", "", [][2]string{{"dbc/Map.dbc", "map labels"}, {"unused/extra", "ignored"}}},
		{"paths cannot escape", "", [][2]string{{"../../Map.dbc", "map labels"}}},
		{"missing", "missing Map.dbc", [][2]string{{"dbc/Other.dbc", "other"}}},
		{"duplicate", "duplicate Map.dbc", [][2]string{{"dbc/Map.dbc", "map labels"}, {"other/Map.dbc", "map labels"}}},
		{"checksum", "checksum mismatch", [][2]string{{"dbc/Map.dbc", "invalid"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			archivePath := filepath.Join(dir, "Data.zip")
			file, err := os.Create(archivePath)
			if err != nil {
				t.Fatal(err)
			}
			archive := zip.NewWriter(file)
			for _, entry := range tc.files {
				writer, err := archive.Create(entry[0])
				if err != nil {
					t.Fatal(err)
				}
				if _, err := io.WriteString(writer, entry[1]); err != nil {
					t.Fatal(err)
				}
			}
			if err := archive.Close(); err != nil {
				t.Fatal(err)
			}
			if err := file.Close(); err != nil {
				t.Fatal(err)
			}
			out := filepath.Join(dir, "output")
			if err := os.Mkdir(out, 0o755); err != nil {
				t.Fatal(err)
			}
			src := testSource("Map.dbc", "map labels")
			err = extract(archivePath, out, []source{src})
			if tc.want != "" {
				if err == nil || !strings.Contains(err.Error(), tc.want) {
					t.Fatalf("got %v, want %q", err, tc.want)
				}
			} else if err != nil || !validFile(filepath.Join(out, src.name), src.sha256) {
				t.Fatal("expected verified extracted data", err)
			}
			if _, err := os.Stat(filepath.Join(dir, "Map.dbc")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("unexpected file outside output directory", err)
			}
		})
	}
}
