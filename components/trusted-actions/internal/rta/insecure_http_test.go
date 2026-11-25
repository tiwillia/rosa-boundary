package rta

import (
	"bytes"
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/spf13/cobra"
	"github.com/spf13/viper"
)

func TestNewClient_InsecureHTTP(t *testing.T) {
	for _, tt := range []struct {
		name   string
		server string
		optIn  bool
		wantOK bool
	}{
		{"default refuses remote HTTP", "http://dev.example.com", false, false},
		{"opt-in allows remote HTTP", "http://dev.example.com", true, true},
		{"default allows HTTPS", "https://dev.example.com", false, true},
		{"opt-in allows HTTPS", "https://dev.example.com", true, true},
		{"default allows loopback HTTP", "http://127.0.0.1:8080", false, true},
		{"opt-in rejects FTP", "ftp://dev.example.com", true, false},
		{"opt-in rejects loopback FTP", "ftp://localhost", true, false},
		{"opt-in rejects file", "file:///tmp/server", true, false},
		{"opt-in rejects missing scheme", "dev.example.com", true, false},
		{"opt-in rejects missing host", "http:///server", true, false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			opts := []ClientOption{WithToken("test-secret-token")}
			if tt.optIn {
				opts = append(opts, WithInsecureHTTP())
			}
			c, err := NewClient(tt.server, opts...)
			if (err == nil) != tt.wantOK {
				t.Fatalf("NewClient error = %v, want success %v", err, tt.wantOK)
			}
			if err != nil {
				if strings.Contains(err.Error(), "test-secret-token") {
					t.Error("error disclosed token")
				}
				return
			}
			if c.allowInsecureHTTP != tt.optIn {
				t.Errorf("allowInsecureHTTP = %v, want %v", c.allowInsecureHTTP, tt.optIn)
			}
		})
	}
}

func TestClient_InsecureHTTP_RejectsHTTPSDowngrade(t *testing.T) {
	var reached atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Store(true)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer target.Close()
	source := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-secret-token" {
			t.Error("initial HTTPS request missing bearer token")
		}
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer source.Close()

	for _, optIn := range []bool{false, true} {
		t.Run(map[bool]string{false: "default", true: "opt-in"}[optIn], func(t *testing.T) {
			hc := source.Client()
			var callbackCalled bool
			hc.CheckRedirect = func(*http.Request, []*http.Request) error {
				callbackCalled = true
				return nil
			}
			opts := []ClientOption{WithToken("test-secret-token"), WithHTTPClient(hc)}
			if optIn {
				opts = append(opts, WithInsecureHTTP())
			}
			c, err := NewClient(source.URL, opts...)
			if err != nil {
				t.Fatal(err)
			}
			_, err = c.Catalog(context.Background())
			if err == nil || !strings.Contains(err.Error(), "refusing redirect from HTTPS") {
				t.Fatalf("expected downgrade refusal, got %v", err)
			}
			if reached.Load() || callbackCalled {
				t.Error("downgrade reached HTTP target or caller redirect policy")
			}
			if strings.Contains(err.Error(), "test-secret-token") {
				t.Error("redirect error disclosed token")
			}
			// The caller's HTTP client must not be modified.
			_ = hc.CheckRedirect(nil, nil)
			if !callbackCalled {
				t.Error("caller redirect policy was replaced")
			}
		})
	}
}

func TestClient_InsecureHTTP_RemoteInitialRequest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != "dev.example.com" {
			t.Errorf("host = %q, want dev.example.com", r.Host)
		}
		if r.Header.Get("Authorization") != "Bearer test-secret-token" {
			t.Error("opt-in request missing bearer token")
		}
		_, _ = w.Write([]byte(`{"items":[],"total":0}`))
	}))
	defer srv.Close()

	// Route the non-loopback URL to the test listener without external traffic.
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, srv.Listener.Addr().String())
		},
	}
	defer transport.CloseIdleConnections()
	c, err := NewClient("http://dev.example.com", WithToken("test-secret-token"),
		WithInsecureHTTP(), WithHTTPClient(&http.Client{Transport: transport}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Catalog(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestClient_InsecureHTTP_PreservesTLSVerification(t *testing.T) {
	var reached atomic.Bool
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Store(true)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	c, err := NewClient(srv.URL, WithToken("test-secret-token"), WithInsecureHTTP())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Catalog(context.Background()); err == nil {
		t.Fatal("expected untrusted TLS certificate to be rejected")
	}
	if reached.Load() {
		t.Error("request reached server with untrusted certificate")
	}
}

func TestCommand_InsecureHTTP_PrecedenceAndWarning(t *testing.T) {
	for _, tt := range []struct {
		name string
		env  string
		flag string
		want bool
	}{
		{"default disabled", "", "", false},
		{"environment enabled", "true", "", true},
		{"environment disabled", "false", "", false},
		{"flag enabled", "", "--allow-insecure-http", true},
		{"flag disabled", "", "--allow-insecure-http=false", false},
		{"flag overrides enabled environment", "true", "--allow-insecure-http=false", false},
		{"flag overrides disabled environment", "false", "--allow-insecure-http", true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("RTA_ALLOW_INSECURE_HTTP", tt.env)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer test-secret-token" {
					t.Error("missing bearer token")
				}
				_, _ = w.Write([]byte(`{"items":[],"total":0}`))
			}))
			defer srv.Close()

			cmd := NewCommand()
			var stdout, stderr bytes.Buffer
			cmd.SetOut(&stdout)
			cmd.SetErr(&stderr)
			args := []string{"action", "list", "--server", srv.URL, "--token", "test-secret-token", "-o", "json"}
			if tt.flag != "" {
				args = append(args, tt.flag)
			}
			cmd.SetArgs(args)
			if err := cmd.Execute(); err != nil {
				t.Fatal(err)
			}
			if got := strings.Contains(stderr.String(), "WARNING: insecure HTTP enabled"); got != tt.want {
				t.Errorf("warning present = %v, want %v; stderr: %s", got, tt.want, stderr.String())
			}
			if tt.want && !strings.Contains(stderr.String(), "unencrypted") {
				t.Error("warning does not explain cleartext risk")
			}
			if strings.Contains(stdout.String(), "WARNING") {
				t.Error("warning polluted stdout")
			}
			if strings.Contains(stdout.String()+stderr.String(), "test-secret-token") {
				t.Error("output disclosed token")
			}
		})
	}
}

func TestCommandGroup_InsecureHTTP(t *testing.T) {
	for _, tt := range []struct {
		name   string
		server string
		optIn  bool
		wantOK bool
		warn   bool
	}{
		{"default remote HTTP refused", "http://dev.example.com", false, false, false},
		{"opt-in remote HTTP allowed", "http://dev.example.com", true, true, true},
		{"opt-in HTTPS has no warning", "https://dev.example.com", true, true, false},
		{"opt-in FTP refused", "ftp://dev.example.com", true, false, false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			config := viper.New()
			config.Set("server", tt.server)
			config.Set("allow_insecure_http", tt.optIn)
			g := &commandGroup{config: config}
			cmd := &cobra.Command{}
			cmd.Flags().String("token", "test-secret-token", "")
			var stderr bytes.Buffer
			cmd.SetErr(&stderr)
			c, err := g.newClient(cmd, false)
			if (err == nil) != tt.wantOK {
				t.Fatalf("newClient error = %v, want success %v", err, tt.wantOK)
			}
			if c != nil && c.allowInsecureHTTP != tt.optIn {
				t.Error("opt-in was not passed to client")
			}
			if got := strings.Contains(stderr.String(), "WARNING"); got != tt.warn {
				t.Errorf("warning present = %v, want %v", got, tt.warn)
			}
			if strings.Contains(stderr.String(), "test-secret-token") {
				t.Error("warning disclosed token")
			}
		})
	}
}
