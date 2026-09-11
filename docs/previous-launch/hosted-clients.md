# Claude Projects and ChatGPT app instructions

Use prompts/project-memory.md as the full instruction block, then add the actual project UUID from onboarding. Connect Hivemind using the platform's supported remote MCP connector/app settings and complete OAuth when required. An arbitrary endpoint URL cannot be installed as a Custom GPT Action: Actions use OpenAPI, while remote MCP is configured through the supported apps/developer workflow. These are separate platform features.

The service only learns information explicitly supplied through commit_memory calls. It cannot inspect historical chats in Claude, ChatGPT, Cursor, or Codex. Tool availability, subscriptions, workspace administrator policy, write confirmations, background execution, and mobile support depend on the native platform. Verify the concrete account/client before selling universal compatibility.

Do not put a personal token in the endpoint URL or the project instruction text. Header-capable clients use Authorization: Bearer with a scoped personal token. Hosted clients use the configured OAuth flow; set up the account-to-identity link during onboarding. The default API-key-only beta deployment does not supply OAuth discovery.
