# How to Build a Personal AI Agent with babo

This guide builds a personal AI agent you can run locally, talk to from the terminal or browser, and later connect to chat apps, memory, tools, and automations.

## What you will build

- a configured babo install
- one working model provider
- one local agent reply
- a browser WebUI session for ongoing work

## When to use this

Use this when you want a personal AI agent that you control rather than a hosted chat-only interface. babo is useful when the agent needs local workspace access, tool calls, session history, memory, scheduled work, or chat app delivery.

## Install

Follow [Quick Start](../quick-start.md) to install babo using the recommended method for your operating system and configure one model. Return here after you receive the first reply. If terminals and config files are new to you, use [Start Without Technical Background](../start-without-technical-background.md) instead.

## Minimal working example

Continue in the WebUI from Quick Start. Start a new topic, select a practice folder and **Restricted** access, and ask babo to summarize a file there. See [Your first file task](../start-without-technical-background.md#your-first-file-task) for a complete example. Reopen the same instance with `babo webui` if it is not running; the launcher starts or joins its gateway.

## Production notes

- Choose a project folder for file work. Use [separate instances](../multiple-instances.md) when identity or long-term memory must be isolated.
- Use `modelPresets` when you want stable names for fast, deep, local, or fallback models.
- Keep `babo gateway` running for WebUI, chat apps, automations, and the WebSocket channel.
- Use the Python SDK or OpenAI-compatible API when another program should call the agent.

## Security notes

- Do not store API keys directly in shared files; use environment variables.
- Prefer chat app pairing for first setup. Use `allowFrom` only for static allowlists, and keep those lists narrow.
- Enable workspace restriction before exposing file or shell tools to other users.
- Use a separate workspace for experiments that can modify files.

## Troubleshooting

- `babo status` shows the config path, workspace path, and active model.
- If the model cannot reply, use **Settings → Models** to correct its connection before adding chat apps.
- If the WebUI opens but does not answer, check gateway logs and provider credentials.

## Related babo docs

- [Quick Start](../quick-start.md)
- [Concepts](../concepts.md)
- [WebUI](../webui.md)
- [Configuration](../configuration.md)
- [Troubleshooting](../troubleshooting.md)
