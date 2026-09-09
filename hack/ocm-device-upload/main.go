// Command ocm-device-upload obtains a short-lived OCM access token locally and
// injects it into a running rosa-boundary task through ECS Exec.
package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"golang.org/x/oauth2"
)

const (
	ocmClientID         = "ocm-cli"
	ocmAPIURL           = "https://api.openshift.com"
	ocmAuthURL          = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/auth"
	ocmDeviceAuthURL    = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/auth/device"
	ocmTokenURL         = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
	ocmRedirectURL      = "http://127.0.0.1:9998/oauth/callback"
	receiverReadyMark   = "__ROSA_BOUNDARY_OCM_UPLOAD_READY__"
	receiverSuccessMark = "__ROSA_BOUNDARY_OCM_UPLOAD_SUCCESS__"
)

// ocmConfig is deliberately limited to the fields needed by the OCM CLI to
// use an access token. In particular, it has no refresh-token field.
type ocmConfig struct {
	AccessToken string   `json:"access_token"`
	ClientID    string   `json:"client_id"`
	Scopes      []string `json:"scopes"`
	TokenURL    string   `json:"token_url"`
	URL         string   `json:"url"`
}

func main() {
	var taskID string
	var flow string
	var joinCommand string
	var joinDirectory string

	flag.StringVar(&taskID, "task-id", "", "Running ECS task ID (required)")
	flag.StringVar(&flow, "flow", "auth-code", "OCM OAuth flow: auth-code or device")
	flag.StringVar(&joinCommand, "rosa-boundary-command", "rosa-boundary", "Command line used to invoke rosa-boundary join-task")
	flag.StringVar(&joinDirectory, "rosa-boundary-directory", "", "Working directory for rosa-boundary command (optional)")
	flag.Parse()

	if taskID == "" {
		fmt.Fprintln(os.Stderr, "Error: --task-id is required")
		flag.Usage()
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	token, err := authenticate(ctx, flow)
	if err != nil {
		fatalf("OCM authentication failed: %v", err)
	}
	if token.AccessToken == "" {
		fatalf("OCM device authentication returned no access token")
	}
	reportExpiration(token.Expiry)

	// Copy only the access token into the JSON that will be sent to the task.
	configJSON, err := json.Marshal(ocmConfig{
		AccessToken: token.AccessToken,
		ClientID:    ocmClientID,
		Scopes:      []string{"openid"},
		TokenURL:    ocmTokenURL,
		URL:         ocmAPIURL,
	})
	if err != nil {
		fatalf("create OCM configuration: %v", err)
	}

	// DeviceAccessToken may return a refresh token. Do not persist or upload it.
	token.RefreshToken = ""
	token.AccessToken = ""

	if err := uploadConfig(ctx, joinCommand, joinDirectory, taskID, configJSON); err != nil {
		fatalf("upload OCM configuration: %v", err)
	}

	fmt.Println("OCM access-token configuration uploaded and verified in the task.")
}

// authenticate obtains an OCM access token using the requested PKCE-protected flow.
func authenticate(ctx context.Context, flow string) (*oauth2.Token, error) {
	switch flow {
	case "auth-code":
		return authenticateAuthCode(ctx)
	case "device":
		return authenticateDevice(ctx)
	default:
		return nil, fmt.Errorf("unsupported --flow %q; use auth-code or device", flow)
	}
}

// newOAuthConfig returns the Red Hat SSO settings shared by both OAuth flows.
func newOAuthConfig() *oauth2.Config {
	return &oauth2.Config{
		ClientID: ocmClientID,
		Scopes:   []string{"openid"},
		Endpoint: oauth2.Endpoint{
			AuthURL:       ocmAuthURL,
			DeviceAuthURL: ocmDeviceAuthURL,
			TokenURL:      ocmTokenURL,
		},
	}
}

// authenticateDevice completes the Red Hat SSO RFC 8628 device flow with PKCE S256.
func authenticateDevice(ctx context.Context) (*oauth2.Token, error) {
	config := newOAuthConfig()

	verifier := oauth2.GenerateVerifier()
	device, err := config.DeviceAuth(ctx,
		oauth2.S256ChallengeOption(verifier),
		oauth2.VerifierOption(verifier),
	)
	if err != nil {
		return nil, fmt.Errorf("request device authorization: %w", err)
	}

	verificationURL, err := deviceVerificationURL(device)
	if err != nil {
		return nil, err
	}
	fmt.Printf("Open this URL to authorize OCM:\n%s\n", verificationURL)
	if device.VerificationURIComplete == "" {
		fmt.Printf("If prompted, enter code: %s\n", device.UserCode)
	}
	if err := openBrowser(verificationURL); err != nil {
		fmt.Fprintf(os.Stderr, "Could not open a browser automatically: %v\n", err)
	}
	fmt.Printf("Waiting for authorization (polling every %d seconds)...\n", pollingInterval(device))

	token, err := config.DeviceAccessToken(ctx, device, oauth2.VerifierOption(verifier))
	if err != nil {
		return nil, fmt.Errorf("poll for access token: %w", err)
	}
	return token, nil
}

// authCodeResult is returned after the loopback callback completes.
type authCodeResult struct {
	token *oauth2.Token
	err   error
}

// authenticateAuthCode performs an authorization-code flow with a loopback
// callback, state validation, and PKCE S256. The redirect URL is the one used
// by OCM CLI's registered public client.
func authenticateAuthCode(ctx context.Context) (*oauth2.Token, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:9998")
	if err != nil {
		return nil, fmt.Errorf("listen for OAuth callback on 127.0.0.1:9998: %w", err)
	}

	config := newOAuthConfig()
	config.RedirectURL = ocmRedirectURL
	state, err := generateState()
	if err != nil {
		_ = listener.Close()
		return nil, err
	}
	verifier := oauth2.GenerateVerifier()
	result := make(chan authCodeResult, 1)

	server := &http.Server{
		Handler: authCodeCallbackHandler(ctx, config, state, verifier, result),
	}
	go func() {
		_ = server.Serve(listener)
	}()
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	authorizationURL := config.AuthCodeURL(state, oauth2.S256ChallengeOption(verifier))
	fmt.Printf("Open this URL to authorize OCM:\n%s\n", authorizationURL)
	if err := openBrowser(authorizationURL); err != nil {
		fmt.Fprintf(os.Stderr, "Could not open a browser automatically: %v\n", err)
	}
	fmt.Println("Waiting for authorization callback on 127.0.0.1:9998...")

	timer := time.NewTimer(5 * time.Minute)
	defer timer.Stop()
	select {
	case result := <-result:
		if result.err != nil {
			return nil, result.err
		}
		return result.token, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-timer.C:
		return nil, errors.New("authorization-code login timed out after 5 minutes")
	}
}

// authCodeCallbackHandler validates the callback before exchanging its code.
func authCodeCallbackHandler(ctx context.Context, config *oauth2.Config, state, verifier string, result chan<- authCodeResult) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if request.URL.Query().Get("state") != state {
			http.Error(w, "invalid OAuth state", http.StatusBadRequest)
			return
		}
		if request.URL.Query().Get("error") != "" {
			http.Error(w, "authorization was not completed", http.StatusBadRequest)
			sendAuthCodeResult(result, authCodeResult{err: errors.New("authorization-code login was denied")})
			return
		}

		code := request.URL.Query().Get("code")
		if code == "" {
			http.Error(w, "authorization response did not include a code", http.StatusBadRequest)
			sendAuthCodeResult(result, authCodeResult{err: errors.New("authorization response did not include a code")})
			return
		}

		token, err := config.Exchange(ctx, code, oauth2.VerifierOption(verifier))
		if err != nil {
			http.Error(w, "authorization-code exchange failed", http.StatusBadGateway)
			sendAuthCodeResult(result, authCodeResult{err: fmt.Errorf("exchange authorization code: %w", err)})
			return
		}

		_, _ = io.WriteString(w, "Login successful. You may close this window and return to the terminal.")
		sendAuthCodeResult(result, authCodeResult{token: token})
	})
}

// sendAuthCodeResult reports only the first completed callback attempt.
func sendAuthCodeResult(result chan<- authCodeResult, value authCodeResult) {
	select {
	case result <- value:
	default:
	}
}

// generateState creates the unpredictable state value used to bind the browser
// redirect to this authorization-code request.
func generateState() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", fmt.Errorf("generate OAuth state: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(bytes), nil
}

// deviceVerificationURL uses the server-provided complete URL, or makes the
// documented user-code URL when that optional response field is absent.
func deviceVerificationURL(device *oauth2.DeviceAuthResponse) (string, error) {
	if device.VerificationURIComplete != "" {
		return device.VerificationURIComplete, nil
	}
	if device.VerificationURI == "" || device.UserCode == "" {
		return "", errors.New("device authorization response has no verification URL or user code")
	}

	verificationURL, err := url.Parse(device.VerificationURI)
	if err != nil {
		return "", fmt.Errorf("parse device verification URL: %w", err)
	}
	query := verificationURL.Query()
	query.Set("user_code", device.UserCode)
	verificationURL.RawQuery = query.Encode()
	return verificationURL.String(), nil
}

// pollingInterval returns the RFC 8628 default when the server omits interval.
func pollingInterval(device *oauth2.DeviceAuthResponse) int64 {
	if device.Interval == 0 {
		return 5
	}
	return device.Interval
}

// openBrowser makes a best-effort attempt to open the device authorization URL.
func openBrowser(url string) error {
	var command *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		command = exec.Command("open", url)
	case "windows":
		command = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		command = exec.Command("xdg-open", url)
	}
	if err := command.Start(); err != nil {
		return err
	}
	go func() {
		_ = command.Wait()
	}()
	return nil
}

// reportExpiration prints only expiry metadata; it never prints token material.
func reportExpiration(expiry time.Time) {
	if expiry.IsZero() {
		fmt.Println("Received OCM access token; expiration was not supplied by the server.")
		return
	}
	fmt.Printf("Received OCM access token; expires at %s (%s remaining).\n", expiry.Format(time.RFC3339), time.Until(expiry).Round(time.Second))
}

// uploadConfig sends the base64-encoded JSON over ECS Exec stdin and confirms
// that the task wrote it and that `ocm whoami` accepted the access token.
func uploadConfig(ctx context.Context, joinCommand, joinDirectory, taskID string, configJSON []byte) error {
	commandParts := strings.Fields(joinCommand)
	if len(commandParts) == 0 {
		return errors.New("--rosa-boundary-command must not be empty")
	}

	args := append(commandParts[1:], "join-task", "--command", receiverCommand, taskID)
	command := exec.CommandContext(ctx, commandParts[0], args...)
	command.Dir = joinDirectory
	stdin, err := command.StdinPipe()
	if err != nil {
		return fmt.Errorf("create ECS Exec stdin pipe: %w", err)
	}
	defer func() { _ = stdin.Close() }()
	stdout, err := command.StdoutPipe()
	if err != nil {
		return fmt.Errorf("create ECS Exec stdout pipe: %w", err)
	}
	command.Stderr = os.Stderr

	if err := command.Start(); err != nil {
		return fmt.Errorf("start rosa-boundary join-task: %w", err)
	}

	if err := sendConfigAndWait(stdout, stdin, configJSON); err != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
		return err
	}
	if err := command.Wait(); err != nil {
		return fmt.Errorf("rosa-boundary join-task exited unsuccessfully: %w", err)
	}
	return nil
}

// sendConfigAndWait waits for the receiver before writing one encoded line,
// then requires its explicit success marker. ECS Exec treats a closed stdin as
// an end-of-session signal, so stdin remains open until the receiver exits.
func sendConfigAndWait(stdout io.Reader, stdin io.WriteCloser, configJSON []byte) error {
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 1024), 1024*1024)
	ready := false
	ok := false

	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.Contains(line, receiverReadyMark):
			if ready {
				return errors.New("task receiver sent READY more than once")
			}
			payload := base64.StdEncoding.EncodeToString(configJSON)
			if _, err := fmt.Fprintln(stdin, payload); err != nil {
				return fmt.Errorf("send OCM configuration to task: %w", err)
			}
			ready = true
		case strings.Contains(line, receiverSuccessMark):
			if ready {
				ok = true
			}
		default:
			fmt.Println(line)
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read task output: %w", err)
	}
	if !ready {
		return errors.New("task receiver did not report READY")
	}
	if !ok {
		return errors.New("task receiver did not report OK; ocm whoami may have failed")
	}
	return nil
}

// receiverCommand runs inside ECS Exec. It switches to sre, reads exactly one
// base64 line without terminal echo, atomically writes a mode-0600 config, and
// validates that the installed OCM CLI can use it.
const receiverCommand = `runuser --user=sre -- sh -c '
set -eu
stty -echo
trap "stty echo" EXIT HUP INT TERM
printf "READY\n"
printf "__ROSA_BOUNDARY_OCM_UPLOAD_READY__\n"
IFS= read -r payload
config_dir=/home/sre/.config/ocm
mkdir --parents "$config_dir"
umask 077
tmp=$(mktemp "$config_dir/ocm.json.XXXXXX")
trap "rm --force \"$tmp\"; stty echo" EXIT HUP INT TERM
printf "%s" "$payload" | base64 --decode > "$tmp"
chmod 600 "$tmp"
mv --force "$tmp" "$config_dir/ocm.json"
unset OCM_KEYRING
HOME=/home/sre XDG_CONFIG_HOME=/home/sre/.config OCM_CONFIG="$config_dir/ocm.json" ocm whoami
printf "OK\n"
printf "__ROSA_BOUNDARY_OCM_UPLOAD_SUCCESS__\n"
'`

// fatalf writes an error without exposing token data and exits with failure.
func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "Error: "+format+"\n", args...)
	os.Exit(1)
}
