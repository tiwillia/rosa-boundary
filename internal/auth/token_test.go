package auth

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// makeJWT builds a minimal unsigned JWT with the given exp claim.
// The header and signature are syntactically valid but not cryptographically
// meaningful — CachedToken does not verify signatures.
func makeJWT(exp int64) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	claims := map[string]any{"exp": exp, "sub": "test-user"}
	claimsJSON, _ := json.Marshal(claims)
	payload := base64.RawURLEncoding.EncodeToString(claimsJSON)
	sig := base64.RawURLEncoding.EncodeToString([]byte("fake-signature"))
	return fmt.Sprintf("%s.%s.%s", header, payload, sig)
}

// setupCacheDir creates a temporary cache directory and sets XDG_CACHE_HOME
// so that CachedToken / SaveToken use it. Returns a cleanup function.
func setupCacheDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CACHE_HOME", dir)
	// Create the rosa-boundary subdirectory (CacheDir creates it, but
	// we need it for direct file writes in tests).
	cacheDir := filepath.Join(dir, "rosa-boundary")
	if err := os.MkdirAll(cacheDir, 0o700); err != nil {
		t.Fatalf("cannot create cache dir: %v", err)
	}
	return cacheDir
}

func writeTokenFile(t *testing.T, cacheDir, token string) {
	t.Helper()
	path := filepath.Join(cacheDir, tokenCacheFile)
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatalf("cannot write token file: %v", err)
	}
}

// --- parseJWTExpiry tests ---

func TestParseJWTExpiry_Valid(t *testing.T) {
	exp := time.Now().Add(10 * time.Minute).Unix()
	token := makeJWT(exp)
	got, err := parseJWTExpiry(token)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Unix() != exp {
		t.Errorf("exp = %d, want %d", got.Unix(), exp)
	}
}

func TestParseJWTExpiry_MalformedJWT(t *testing.T) {
	_, err := parseJWTExpiry("not-a-jwt")
	if err == nil {
		t.Fatal("expected error for malformed JWT")
	}
}

func TestParseJWTExpiry_NoExpClaim(t *testing.T) {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"sub":"test"}`))
	sig := base64.RawURLEncoding.EncodeToString([]byte("sig"))
	token := fmt.Sprintf("%s.%s.%s", header, payload, sig)
	_, err := parseJWTExpiry(token)
	if err == nil {
		t.Fatal("expected error for missing exp claim")
	}
}

func TestParseJWTExpiry_InvalidBase64(t *testing.T) {
	token := "header.!!!invalid-base64!!!.sig"
	_, err := parseJWTExpiry(token)
	if err == nil {
		t.Fatal("expected error for invalid base64")
	}
}

// --- CachedToken tests ---

func TestCachedToken_NoFile(t *testing.T) {
	_ = setupCacheDir(t)
	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "" {
		t.Errorf("expected empty token, got %q", token)
	}
}

func TestCachedToken_ValidToken(t *testing.T) {
	cacheDir := setupCacheDir(t)
	exp := time.Now().Add(10 * time.Minute).Unix()
	jwt := makeJWT(exp)
	writeTokenFile(t, cacheDir, jwt)

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != jwt {
		t.Error("expected cached token to be returned")
	}
}

func TestCachedToken_ExpiredToken(t *testing.T) {
	cacheDir := setupCacheDir(t)
	// Token expired 1 minute ago
	exp := time.Now().Add(-1 * time.Minute).Unix()
	jwt := makeJWT(exp)
	writeTokenFile(t, cacheDir, jwt)

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "" {
		t.Error("expected empty token for expired JWT")
	}
}

func TestCachedToken_ExpiresWithinBuffer(t *testing.T) {
	cacheDir := setupCacheDir(t)
	// Token expires in 3 seconds — within the 5-second buffer
	exp := time.Now().Add(3 * time.Second).Unix()
	jwt := makeJWT(exp)
	writeTokenFile(t, cacheDir, jwt)

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "" {
		t.Error("expected empty token for JWT expiring within buffer")
	}
}

func TestCachedToken_ExpiresJustOutsideBuffer(t *testing.T) {
	cacheDir := setupCacheDir(t)
	// Token expires in 30 seconds — well outside the 5-second buffer
	exp := time.Now().Add(30 * time.Second).Unix()
	jwt := makeJWT(exp)
	writeTokenFile(t, cacheDir, jwt)

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != jwt {
		t.Error("expected cached token to be returned for JWT expiring outside buffer")
	}
}

func TestCachedToken_EmptyFile(t *testing.T) {
	cacheDir := setupCacheDir(t)
	writeTokenFile(t, cacheDir, "")

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "" {
		t.Error("expected empty token for empty cache file")
	}
}

func TestCachedToken_UnparseableToken(t *testing.T) {
	cacheDir := setupCacheDir(t)
	writeTokenFile(t, cacheDir, "not-a-jwt")

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "" {
		t.Error("expected empty token for unparseable JWT (cache miss)")
	}
}

// --- Expiry-based caching validation ---

func TestCachedToken_UsesJWTExpNotFileMtime(t *testing.T) {
	cacheDir := setupCacheDir(t)

	// Create a token that expires 30 minutes from now.
	exp := time.Now().Add(30 * time.Minute).Unix()
	jwt := makeJWT(exp)
	writeTokenFile(t, cacheDir, jwt)

	// Backdate the file's mtime to 10 minutes ago. Under the old mtime-based
	// logic (4-minute validity window), this file would be considered stale.
	// Under the new exp-based logic, the token is still valid for ~30 minutes.
	staleTime := time.Now().Add(-10 * time.Minute)
	cachePath := filepath.Join(cacheDir, tokenCacheFile)
	if err := os.Chtimes(cachePath, staleTime, staleTime); err != nil {
		t.Fatalf("cannot backdate file mtime: %v", err)
	}

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != jwt {
		t.Error("expected cached token to be returned: expiry is based on JWT exp, not file mtime")
	}
}

// --- SaveToken / ClearToken round-trip tests ---

func TestSaveToken_RoundTrip(t *testing.T) {
	_ = setupCacheDir(t)
	exp := time.Now().Add(10 * time.Minute).Unix()
	jwt := makeJWT(exp)

	if err := SaveToken(jwt); err != nil {
		t.Fatalf("SaveToken failed: %v", err)
	}

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("CachedToken failed: %v", err)
	}
	if token != jwt {
		t.Error("round-trip failed: cached token does not match saved token")
	}
}

func TestClearToken(t *testing.T) {
	_ = setupCacheDir(t)
	exp := time.Now().Add(10 * time.Minute).Unix()
	jwt := makeJWT(exp)

	if err := SaveToken(jwt); err != nil {
		t.Fatalf("SaveToken failed: %v", err)
	}
	if err := ClearToken(); err != nil {
		t.Fatalf("ClearToken failed: %v", err)
	}

	token, err := CachedToken()
	if err != nil {
		t.Fatalf("CachedToken failed: %v", err)
	}
	if token != "" {
		t.Error("expected empty token after ClearToken")
	}
}

func TestClearToken_NoFile(t *testing.T) {
	_ = setupCacheDir(t)
	// Clearing a non-existent cache should not error
	if err := ClearToken(); err != nil {
		t.Fatalf("ClearToken failed on non-existent file: %v", err)
	}
}
