package thinking_test

import (
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/thinking"
	_ "github.com/router-for-me/CLIProxyAPI/v7/internal/thinking/provider/claude"
	_ "github.com/router-for-me/CLIProxyAPI/v7/internal/thinking/provider/codex"
	"github.com/tidwall/gjson"
)

func TestApplyThinking_UserDefinedClaudePreservesAdaptiveLevel(t *testing.T) {
	reg := registry.GetGlobalRegistry()
	clientID := "test-user-defined-claude-" + t.Name()
	modelID := "custom-claude-4-6"
	reg.RegisterClient(clientID, "claude", []*registry.ModelInfo{{ID: modelID, UserDefined: true}})
	t.Cleanup(func() {
		reg.UnregisterClient(clientID)
	})

	tests := []struct {
		name  string
		model string
		body  []byte
	}{
		{
			name:  "claude adaptive effort body",
			model: modelID,
			body:  []byte(`{"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}`),
		},
		{
			name:  "suffix level",
			model: modelID + "(high)",
			body:  []byte(`{}`),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			out, err := thinking.ApplyThinking(tt.body, tt.model, "openai", "claude", "claude")
			if err != nil {
				t.Fatalf("ApplyThinking() error = %v", err)
			}
			if got := gjson.GetBytes(out, "thinking.type").String(); got != "adaptive" {
				t.Fatalf("thinking.type = %q, want %q, body=%s", got, "adaptive", string(out))
			}
			if got := gjson.GetBytes(out, "output_config.effort").String(); got != "high" {
				t.Fatalf("output_config.effort = %q, want %q, body=%s", got, "high", string(out))
			}
			if gjson.GetBytes(out, "thinking.budget_tokens").Exists() {
				t.Fatalf("thinking.budget_tokens should be removed, body=%s", string(out))
			}
		})
	}
}

// TestApplyThinking_UserDefinedCodexClampsUnsupportedLevel verifies that a
// user-defined codex alias (e.g. claude-opus-4-7 aliased onto a GPT-5 family
// model) clamps a requested level that isn't in the underlying model's
// supported Levels list. This is the codex-api-key bypass path: ApplyThinking
// detours through applyUserDefinedModel and skips ValidateConfig, so without
// the clamp the unsupported value would be forwarded as-is to the upstream.
func TestApplyThinking_UserDefinedCodexClampsUnsupportedLevel(t *testing.T) {
	reg := registry.GetGlobalRegistry()
	clientID := "test-user-defined-codex-clamp"
	modelID := "claude-opus-4-7-codex-alias"
	reg.RegisterClient(clientID, "codex", []*registry.ModelInfo{{
		ID:          modelID,
		UserDefined: true,
		Thinking: &registry.ThinkingSupport{
			Levels: []string{"low", "medium", "high", "xhigh"},
		},
	}})
	t.Cleanup(func() {
		reg.UnregisterClient(clientID)
	})

	tests := []struct {
		name       string
		body       []byte
		wantEffort string
	}{
		{
			name:       "max is clamped to xhigh",
			body:       []byte(`{"reasoning":{"effort":"max"}}`),
			wantEffort: "xhigh",
		},
		{
			name:       "high passes through unchanged",
			body:       []byte(`{"reasoning":{"effort":"high"}}`),
			wantEffort: "high",
		},
		{
			name:       "medium passes through unchanged",
			body:       []byte(`{"reasoning":{"effort":"medium"}}`),
			wantEffort: "medium",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			out, err := thinking.ApplyThinking(tt.body, modelID, "codex", "codex", "codex")
			if err != nil {
				t.Fatalf("ApplyThinking() error = %v", err)
			}
			if got := gjson.GetBytes(out, "reasoning.effort").String(); got != tt.wantEffort {
				t.Fatalf("reasoning.effort = %q, want %q, body=%s", got, tt.wantEffort, string(out))
			}
		})
	}
}

// TestApplyThinking_UserDefinedCodexLevelsIncludeMaxPreserved verifies that
// when the user-defined codex model's Levels do include "max", the level is
// preserved (no spurious clamping).
func TestApplyThinking_UserDefinedCodexLevelsIncludeMaxPreserved(t *testing.T) {
	reg := registry.GetGlobalRegistry()
	clientID := "test-user-defined-codex-keep-max"
	modelID := "future-codex-supports-max"
	reg.RegisterClient(clientID, "codex", []*registry.ModelInfo{{
		ID:          modelID,
		UserDefined: true,
		Thinking: &registry.ThinkingSupport{
			Levels: []string{"low", "medium", "high", "xhigh", "max"},
		},
	}})
	t.Cleanup(func() {
		reg.UnregisterClient(clientID)
	})

	out, err := thinking.ApplyThinking([]byte(`{"reasoning":{"effort":"max"}}`), modelID, "codex", "codex", "codex")
	if err != nil {
		t.Fatalf("ApplyThinking() error = %v", err)
	}
	if got := gjson.GetBytes(out, "reasoning.effort").String(); got != "max" {
		t.Fatalf("reasoning.effort = %q, want %q, body=%s", got, "max", string(out))
	}
}
