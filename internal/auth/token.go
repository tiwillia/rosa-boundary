package auth

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/openshift/rosa-boundary/internal/config"
)

const (
	tokenCacheFile = "token-cache"
	// expiryBuffer is subtracted from the JWT exp claim to avoid using
	// a token that is about to expire mid-request.
	expiryBuffer = 5 * time.Second
)

// jwtClaims holds the subset of JWT claims needed for cache validation.
type jwtClaims struct {
	Exp int64 `json:"exp"`
}

// parseJWTExpiry extracts the exp claim from a JWT without verifying the
// signature. Validation is performed server-side; the cache only needs the
// expiry time to decide whether to reuse the token.
func parseJWTExpiry(token string) (time.Time, error) {
	parts := strings.SplitN(token, ".", 3)
	if len(parts) != 3 {
		return time.Time{}, fmt.Errorf("malformed JWT: expected 3 parts, got %d", len(parts))
	}

	// JWT base64url may lack padding; add it back.
	payload := parts[1]
	if m := len(payload) % 4; m != 0 {
		payload += strings.Repeat("=", 4-m)
	}

	decoded, err := base64.URLEncoding.DecodeString(payload)
	if err != nil {
		return time.Time{}, fmt.Errorf("cannot decode JWT payload: %w", err)
	}

	var claims jwtClaims
	if err := json.Unmarshal(decoded, &claims); err != nil {
		return time.Time{}, fmt.Errorf("cannot parse JWT claims: %w", err)
	}
	if claims.Exp == 0 {
		return time.Time{}, fmt.Errorf("JWT has no exp claim")
	}

	return time.Unix(claims.Exp, 0), nil
}

// CachedToken reads the token from cache if it has not yet expired.
// Expiry is determined from the JWT exp claim with a small buffer.
// Returns empty string and nil error if there is no valid cached token.
func CachedToken() (string, error) {
	cacheDir, err := config.CacheDir()
	if err != nil {
		return "", err
	}
	cachePath := filepath.Join(cacheDir, tokenCacheFile)

	if _, err := os.Stat(cachePath); err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", fmt.Errorf("cannot stat token cache: %w", err)
	}

	data, err := os.ReadFile(cachePath)
	if err != nil {
		return "", fmt.Errorf("cannot read token cache: %w", err)
	}

	token := string(data)
	if token == "" {
		return "", nil
	}

	expiry, err := parseJWTExpiry(token)
	if err != nil {
		// Token is unparseable; treat as cache miss so a fresh login occurs.
		return "", nil
	}

	remaining := time.Until(expiry) - expiryBuffer
	if remaining <= 0 {
		return "", nil
	}

	fmt.Fprintf(os.Stderr, "Using cached token (%d seconds remaining)\n", int(remaining.Seconds()))
	return token, nil
}

// SaveToken writes the token to the cache file.
func SaveToken(token string) error {
	cacheDir, err := config.CacheDir()
	if err != nil {
		return err
	}
	cachePath := filepath.Join(cacheDir, tokenCacheFile)

	if err := os.WriteFile(cachePath, []byte(token), 0o600); err != nil {
		return fmt.Errorf("cannot write token cache: %w", err)
	}
	return nil
}

// ClearToken removes the cached token.
func ClearToken() error {
	cacheDir, err := config.CacheDir()
	if err != nil {
		return err
	}
	cachePath := filepath.Join(cacheDir, tokenCacheFile)
	if err := os.Remove(cachePath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("cannot remove token cache: %w", err)
	}
	return nil
}
