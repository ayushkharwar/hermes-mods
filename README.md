# hermes-mods

Three small mods for [Hermes Agent](https://hermes-agent.nousresearch.com/docs): a live session
cost panel, a file activity rail with a navigable explorer, and a prompt cache warmer.

Every mod is a Hermes plugin (Python) plus, where needed, a TUI widget (plain ESM).
They were built against Hermes Agent 0.23.1 and are installed by copying files, no fork and
no patch of the Hermes source.

## What you get

| Mod | Type | What it does |
|---|---|---|
| `savvy-progress` | plugin + widget | Live card for your current chat: model, tokens, cache hit rate, context usage, cost, burn rate, cache savings, today's spend across every session, and the cost of delegate children that chat spawned |
| `filetree-rail` | plugin + widget | Ambient rail of files the agent read or wrote, plus a modal `/explorer` with keyboard navigation and a file preview |
| `keepwarm` | plugin | `/keepwarm` replays the session's last API request with `max_tokens=1` on a timer, so the provider prompt cache stays warm until `/keepwarm-stop` |

## Install

```bash
git clone https://github.com/ayushkharwar/hermes-mods.git
cd hermes-mods
bash scripts/install.sh
```

`install.sh` copies the plugins into `$HERMES_HOME/plugins/`, the widgets into
`$HERMES_HOME/tui-widgets/`, and enables each plugin. The Hermes gateway reloads plugins on
`enable`, so no restart is usually needed. Widgets hot-load into a running TUI within a second.

The `savvy-progress` card and `/progress` exist only in `hermes --tui`; `/svprogress` prints
the same snapshot as text in either interface.

Then, inside `hermes --tui`:

```
/progress      toggle the cost card (it also docks itself on load)
/filetree      toggle the file rail (ambient, mouse clickable)
/explorer      open the modal file browser: j/k move, g/G jump, enter preview, esc close
/svprogress    print the card as text
/keepwarm      start warming every 240 seconds, /keepwarm 120 for a custom interval
/keepwarm-stop stop warming
```

## Verify

```bash
bash scripts/verify.sh
```

Runs every test suite offline. No network, no live API calls, no writes outside a temp dir.

## How it works

```
               ┌──────────────────────────┐
  Hermes turn  │ plugin hook              │
  ────────────►│ post_tool_call           │
               │ post_api_request         │
               └───────────┬──────────────┘
                           │ reads state.db (read only)
                           ▼
               ┌──────────────────────────┐
               │ $HERMES_HOME/state/      │
               │   savvy-progress.json    │
               │   <session>.json         │
               └───────────┬──────────────┘
                           │ polled every 500 ms
                           ▼
               ┌──────────────────────────┐
               │ tui-widgets/*.mjs        │
               │ ambient card, docked     │
               └──────────────────────────┘
```

- The plugin never talks to the widget directly. One JSON file is the contract.
- Session scoping: the card follows the session that fired the hook, restricted to
  `created_source = 'tui'` (your own chat). Delegate children are counted as descendants of that
  session only, depth 3, so one chat never inherits another chat's spend.
- Cost resolution order: provider `actual_cost_usd`, then `estimated_cost_usd`, then a local
  price table. The card marks table prices with `~`.
- `keepwarm` refuses to replay a truncated payload. Hermes caps hook payloads at
  `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` (default 50000), so raise that cap in `$HERMES_HOME/.env`
  if your request bodies are bigger, and restart the gateway.

## Layout

```
plugins/        Hermes plugins, one directory each
tui-widgets/    ESM widget apps
tests/          offline suites, one directory per mod
scripts/        install.sh, verify.sh
docs/           build reports and smoke logs per mod
```

## Limits

- Widgets render only in the Ink TUI (`hermes --tui`). The classic CLI and messaging platforms
  show nothing.
- Ambient widgets receive no keyboard input by design, so navigation lives in the modal explorer.
- The cost card follows the last `tui` session that made an API call. Switching chat without
  sending a message keeps the previous chat's numbers on screen until your first turn lands.
- Prices ship as a small table for the DeepSeek lane used during development. Edit
  `PRICE_TABLE` in `plugins/savvy-progress/__init__.py` for your own provider.

## License

MIT. See [LICENSE](LICENSE).
