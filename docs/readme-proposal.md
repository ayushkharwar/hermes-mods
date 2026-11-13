# hermes-mods

Session costs, file activity, and prompt cache warming for Hermes Agent.

[Install](#60-second-install) · [Progress](#savvy-progress) · [Files](#filetree-rail) · [Cache](#keepwarm) · [Failures](#fallbacks-and-failures) · [Develop](#development)

## See the output

**The widget**

Rendered from the [golden session fixture](tests/savvy-progress/golden_single_session.json), with optional rows hidden.

```text
PROGRESS
model   deepseek-v4.1-flash
tokens         242.9k / 104.9k
cache       ████████████ 98.7%
ctx         ██░░░░░░░░░░ 13.3%
cost                  ~$0.3074
burn                  ~$0.55/h
saved                 ~$5.3245
today       ~$1.4790  24 chats
tok/day                  85.0M
codex               $n/a 24.1M
pi            ~$1.6343? 111.3M
dsh            ~$3.0715? 93.8M
others        ~$4.7058? 229.3M
agents                    none
```

Copied from a live card.
`$n/a` means no price exists for that model.
`?` means part of that total is unpriced and left out of the money.

**The same card through `/svprogress`**, abridged after the rows:

```text
PROGRESS  deepseek-v4.1-flash  custom
────────────────────────────────────────
  primary       d4d9e9539f70  12 agents
  api calls               99
  input              145,960   ◐ 98.1%
  output              73,154
  cache read       7,741,696
  context         92.7k/1.0M    9.3%
  cost              ~$0.3543   list price
  burn/hour         ~$0.56/h
  cache saved       ~$2.2761   list price
  today             ~$1.4882   24 chats
  tokens/day      85,534,200
  agents                  12   +~$0.1763
  codex   4 models         24.1M      $n/a
          · gpt-6.1-sol    14.2M      $n/a
  pi      15 models       111.3M ~$1.6343?
          · DeepSeek-V4.1… 89.1M  ~$1.4203
  dsh     3 models         93.8M ~$3.0715?
  total 3 all models      229.3M ~$4.7058?
════════════════════════════════════════
        (12 turn lines and the stale note follow)
```

This machine has real codex, pi and dsh history, so those rows carry real numbers.

**A real verifier result**

```text
PASS  savvy-progress plugin
PASS  savvy-progress widget
PASS  savvy-progress integration
PASS  savvy-progress hardening
PASS  savvy-progress agents
PASS  savvy-progress rows
PASS  savvy-progress concurrency
PASS  savvy-progress soak
PASS  filetree-rail plugin
PASS  filetree-rail widget
PASS  keepwarm capture
PASS  keepwarm ping
PASS  keepwarm stop
PASS  keepwarm truncated
PASS  keepwarm fallback
PASS  keepwarm arm

verify: all suites passed
```

Measured on this machine: **16 suites, 16 passed**.


## Three mods

| Mod | Surface | Purpose |
| :--- | :--- | :--- |
| [savvy-progress](#savvy-progress) | Plugin + TUI widget | Track session tokens, cache use, costs, delegates, and local agent spend. |
| [filetree-rail](#filetree-rail) | Plugin + TUI widget | Follow touched files and browse them in a modal explorer. |
| [keepwarm](#keepwarm) | Plugin | Replay the last captured request on a timer to warm the provider cache. |

## 60-second install

Requires Hermes Agent on `PATH`.
Widgets require its Ink TUI.

```bash
git clone https://github.com/ayushkharwar/hermes-mods.git
cd hermes-mods
bash scripts/install.sh
hermes --tui
```

| Destination | Installed files |
| :--- | :--- |
| `$HERMES_HOME/plugins/` | `savvy-progress`, `filetree-rail`, `keepwarm` |
| `$HERMES_HOME/tui-widgets/` | `savvy-progress.mjs`, `filetree.mjs` |

`HERMES_HOME` defaults to `~/.hermes`.
The installer copies files and enables plugins without patching Hermes.
Check for enable failures, even if it prints `Done`.

Send a normal message to populate state.
Paste slash commands at the Hermes terminal prompt, one line at a time.


## savvy-progress

The widget polls its snapshot every 500 ms.

Print the card in the TUI or classic CLI:

```text
/svprogress
```

The widget docks itself in the TUI. There is no toggle command.

Hide optional rows, then restore them:

```text
/svprogress hide burn saved today daytok codex pi dsh others
/svprogress reset
```

| Reading | Meaning |
| :--- | :--- |
| `~$` | Price table estimate. |
| `$n/a` | No price available. |
| `?` on other-agent totals | Unpriced usage is excluded from the money total. |
| `today`, `tok/day` | Spend and tokens for sessions started today, by local calendar. |
| `agents` | Delegate children of the tracked chat. |
| `codex`, `pi`, `dsh`, `others` | Local agent transcript totals and their combined total. |

Prices and row controls live in the [plugin reference](plugins/savvy-progress/README.md).


## filetree-rail

Rendered after one `read_file` hook for `README.md`.
Blank rows omitted:

```text
FILES                           r1 w0 e0
○ README.md                            1
```

The rail shows five files from a log capped at 40 files and 200 events.
It records `read_file`, `write_file`, `patch`, and `search_files` paths.
It ignores `terminal` calls.
The log spans sessions.

Toggle, browse, or clear:

```text
/filetree
/explorer
/filetree-clear
```

| Explorer key | Action |
| :--- | :--- |
| `j` / `k`, arrow keys | Move selection. |
| `g` / `G` | Jump to first / last file. |
| Enter / Space | Preview up to 60 lines. |
| Escape | Close preview, then close explorer. |
| `r` | Reload the file list. |


## keepwarm

The status renderer before capture:

```text
Keepwarm      stopped
Interval      240 s
Pings         0 / 60
OK / failed   0 / 0
Cache reads   0 tok
Cost (est)    not priced
Prompt tok    0
Last ping     -
Started       -
```

Start at the default 240-second interval:

```text
/keepwarm
```

Change to 120 seconds:

```text
/keepwarm-stop
/keepwarm 120
```

Stop warming:

```text
/keepwarm-stop
```

Each ping makes a provider request with `max_tokens=1` and `stream=false`.
Provider charges can apply.
The loop clamps intervals to 60 through 1800 seconds.
It stops at 60 pings, 3 hours, or 3 consecutive failures.
The capture stores the request body with mode `0600`.
The API key stays out of the capture.


## Data flow

```text
Hermes API / tool hooks
          |
          +--> savvy-progress <-- state.db (read only)
          |         |
          |         +--> state/savvy-progress.json --> progress widget
          |
          +--> filetree-rail
          |         |
          |         +--> state/filetree.json --> rail / explorer
          |
          +--> keepwarm --> state/keepwarm-request.json
                                   |
                                   +--> timed provider replay
                                   +--> state/keepwarm-status.json
```

State paths above sit under `$HERMES_HOME`.


## Fallbacks and failures

| Condition | Result |
| :--- | :--- |
| Classic CLI | No widgets; use `/svprogress` for text. |
| Switch chats without a new API call | Progress can still show the previous chat. |
| Missing, corrupt, or locked progress database | Card uses zeros and `n/a`. |
| Unreadable progress snapshot | Widget keeps its last good card. |
| Progress snapshot reaches 120 seconds old | Widget title shows its age. |
| Missing or malformed filetree snapshot | Empty rail; explorer reports the issue. |
| Missing local transcripts or `zstd` | The corresponding other-agent data is unavailable. |
| Keepwarm has no capture | Requests a normal message first. |
| Keepwarm capture is truncated | Replay is refused; error names `HERMES_PLUGIN_PAYLOAD_MAX_CHARS`. |
| Provider returns HTTP 400, 413, or 422 | Retries without tools, then without `reasoning_effort`; preserves messages. |


## Development

Use Python 3 and Node.js.
Run the [verifier](scripts/verify.sh):

```bash
bash scripts/verify.sh
```

It runs 16 suites from [tests](tests/).
It writes per-suite logs under `/tmp/verify-*.log`.
Keepwarm tests use localhost fake providers, not live APIs.
The progress plugin smoke test uses the active Hermes home and rewrites its progress state.

Two suites need a normal machine:

| Condition | Suites that fail |
| :--- | :--- |
| `$HERMES_HOME/state` is read-only | progress plugin, integration, agent train |
| Localhost sockets are blocked | keepwarm ping, fallback, arm |

Both write temporary state and talk to a fake local provider.
Neither touches a live API.

Extend [Python plugins](plugins/) and [ESM widgets](tui-widgets/).
Keep their JSON snapshot contracts aligned.

[MIT license](LICENSE).
