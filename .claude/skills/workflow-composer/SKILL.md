---
name: workflow-composer
description: |
  Author and validate n8n workflow JSON before submitting to n8n_create_workflow.
  Use when the user asks to build, compose, automate, or wire up an n8n
  workflow — anything involving triggers, nodes, scheduling, branching,
  webhooks, integrations, or "make me a flow that...". Auto-triggers on
  words: workflow, automation, n8n, trigger, cron, webhook, schedule, "wire
  up", "automate when", "every time X happens". Always lists existing
  credentials and node types from the live n8n instance before composing,
  validates the workflow JSON shape, and creates as inactive by default.
license: MIT
---

# Workflow Composer

Compose valid n8n workflow JSON safely. Never guess credential IDs or node
types — query the live instance first, then compose against what actually
exists.

## When this skill activates

Any user request that maps to an n8n automation: "send me a Slack message
when X", "every morning at 8 send a digest of Y", "when a webhook hits Z do
A then B", "make a flow that …". Also activates when the user says the words
**workflow, automation, n8n, trigger, cron, webhook, schedule**.

## Mandatory pre-flight (always, in this order)

1. **Call `n8n_list_credentials` first** to discover what's connected.
   Don't assume `slack_oauth_1` exists; the real ID is something like
   `cred_8f2a…`.
2. **Call `n8n_list_workflows`** so you can match the user's naming and
   tagging convention. New workflows should look like they belong.
3. **Call `n8n_list_credential_types`** if the user asks for an integration
   we haven't wired up — the correct credential type name (`slackOAuth2Api`
   vs `slackApi`) matters for node binding.

If any of those calls fail, surface the error and stop — don't compose
against unknown state.

## Workflow JSON shape (n8n v1.x)

```json
{
  "name": "<descriptive>",
  "nodes": [
    {
      "id": "<uuid>",
      "name": "<human-readable>",
      "type": "<node-type>",
      "typeVersion": <int>,
      "position": [<x>, <y>],
      "parameters": { ... },
      "credentials": { "<credentialType>": { "id": "<credId>", "name": "<credName>" } }
    }
  ],
  "connections": {
    "<source-node-name>": {
      "main": [[ { "node": "<target-node-name>", "type": "main", "index": 0 } ]]
    }
  },
  "settings": { "executionOrder": "v1" },
  "active": false
}
```

### Common node types worth memorizing

| Purpose | type | typeVersion |
|---|---|---|
| Manual trigger (test) | `n8n-nodes-base.manualTrigger` | 1 |
| Cron schedule | `n8n-nodes-base.scheduleTrigger` | 1.2 |
| Webhook | `n8n-nodes-base.webhook` | 2 |
| HTTP request | `n8n-nodes-base.httpRequest` | 4.2 |
| Code (JS) | `n8n-nodes-base.code` | 2 |
| Set / edit fields | `n8n-nodes-base.set` | 3.4 |
| If branching | `n8n-nodes-base.if` | 2 |
| Slack send | `n8n-nodes-base.slack` | 2.2 |
| Gmail send | `n8n-nodes-base.gmail` | 2.1 |

If the user wants something else, call `n8n_list_node_types` to look up the
exact `type` and current `typeVersion` rather than guess. Wrong typeVersions
silently break params.

## Composition rules

1. **Inactive by default.** Set `"active": false` on every workflow you
   create. The user activates explicitly after testing. This is non-negotiable
   — an active workflow with bad config can spam Slack/email/customers
   instantly.
2. **Wire credentials by ID, not by name.** Names can collide; IDs are
   unique. Get them from step 1.
3. **One trigger per workflow.** Multi-trigger flows are legal but confusing
   — split them unless the user explicitly asks for one workflow.
4. **Position nodes left-to-right.** `[250, 300]`, `[450, 300]`, `[650, 300]`
   reads naturally when the user opens the editor.
5. **Name nodes for what they do, not what they are.** "Notify on-call
   Slack" beats "Slack1".
6. **Echo the JSON back to the user** before calling `n8n_create_workflow`
   so they can spot mistakes. Wait for explicit "yes create it".

## Validation checklist (run before n8n_create_workflow)

- [ ] All `credentials.<type>.id` values came from `n8n_list_credentials`.
- [ ] Every node referenced in `connections` exists in `nodes`.
- [ ] Every node has a unique `name`.
- [ ] Exactly one trigger node (manualTrigger / webhook / scheduleTrigger).
- [ ] `active: false`.
- [ ] `settings.executionOrder: "v1"` (legacy "v0" is broken for many newer
      nodes).
- [ ] No literal secrets baked into `parameters` — they should reference
      `{{ $credentials.* }}` or `{{ $env.* }}`.

If any check fails, fix before submitting. Don't ask the user to fix what
you can fix yourself.

## After creation

1. Print the workflow URL (`<n8n_base>/workflow/<id>`) so the user can open
   it.
2. Suggest a test run via `n8n_execute_workflow` for manual triggers, or
   tell the user how to fire the webhook for webhook triggers.
3. Only suggest activating after a successful test run.

## What this skill does NOT do

- Mutates existing workflows. If the user asks to edit, use the dedicated
  edit tools (`n8n_update_workflow`); don't recreate.
- Manages credentials. Creating/rotating creds is its own workflow — refer
  the user to the n8n UI.
- Bypasses confirmation. `n8n_create_workflow` is `requires_confirmation:
  true` for a reason.
