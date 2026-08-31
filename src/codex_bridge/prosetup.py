"""One command wires ChatGPT Pro into Codex as a model provider.

Codex reads custom providers from ~/.codex/config.toml. This appends one
provider block pointing at the bridge's /v1 endpoint and one profile that
selects it, so `codex --profile pro` runs any Codex workflow with the app
subscription's Pro as the model. The file is backed up before the first
write and never rewritten in place: blocks are appended only when absent,
and an existing block is left exactly as the user has it.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .auth import load_token

PROVIDER_ID = "chatgpt_pro"
PROFILE = "pro"


def _provider_block(port: int) -> str:
    return f'''
# --- added by codex-remote pro-setup ---
[model_providers.{PROVIDER_ID}]
name = "ChatGPT Pro (app subscription via codex-remote)"
base_url = "http://127.0.0.1:{port}/v1"
env_key = "CODEX_REMOTE_TOKEN"
wire_api = "responses"
# --- end codex-remote pro-setup ---
'''


# Codex >= 0.151 keeps each profile in its own file and rejects a legacy
# [profiles.X] table in config.toml.
def _profile_file_body() -> str:
    return f'''model = "chatgpt-pro"
model_provider = "{PROVIDER_ID}"
'''


def run_setup(port: int = 8788, dry_run: bool = False) -> int:
    config = Path.home() / ".codex" / "config.toml"
    profile_file = Path.home() / ".codex" / f"{PROFILE}.config.toml"
    blocks = _provider_block(port)
    existing = config.read_text() if config.exists() else ""

    if dry_run:
        print(f"would append to {config}:")
        print(blocks)
        print(f"would write {profile_file}:")
        print(_profile_file_body())
        return 0

    if f"[model_providers.{PROVIDER_ID}]" in existing:
        print(f"{config} already has [model_providers.{PROVIDER_ID}]; "
              "leaving it as it is")
    else:
        if existing:
            backup = config.with_name(
                f"config.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(config, backup)
            print(f"backed up {config} -> {backup}")
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("a") as f:
            f.write(blocks)
        print(f"appended provider to {config}")

    if profile_file.exists():
        print(f"{profile_file} exists; leaving it as it is")
    else:
        profile_file.write_text(_profile_file_body())
        print(f"wrote profile {profile_file}")

    token = load_token()
    print()
    print("Codex sends the bridge token from the CODEX_REMOTE_TOKEN "
          "environment variable. Add to your shell profile:")
    if token:
        print(f'  export CODEX_REMOTE_TOKEN="{token}"')
    else:
        print("  export CODEX_REMOTE_TOKEN=\"$(codex-remote token show)\"")
    print()
    print("Then, with the daemon running and a responder available")
    print("(codex-remote respond --watch, a ChatGPT scheduled task, or a")
    print("human saying 'check advice'):")
    print(f"  codex --profile {PROFILE}          # interactive, Pro as the model")
    print(f"  codex exec --profile {PROFILE} \"...\"  # one-shot")
    print()
    print("Expect minutes per reply: this is the app's Pro tier thinking, "
          "priced into your subscription instead of your API usage.")
    return 0


# -- app-free operation ----------------------------------------------------

TASK_PROMPT = """Use the Codex Session Bridge. Check the advice-mailbox \
session for pending requests. For each pending request, read it fully and \
send one answer through the mailbox using the exact required reply \
envelope: the first line is id:<request-id> and every following line is \
the answer. If nothing is pending, do nothing and say nothing."""


def print_task_prompt(interval_minutes: int = 10) -> int:
    """The scheduled-task recipe: the only fully app-free answering path.

    A ChatGPT scheduled task runs on OpenAI's servers, so once it exists
    the desktop app never has to be open and no local process types
    anywhere. Creating the task is the one step that needs the app.
    """
    # TTL must cover: the gap to the next run, plus the longest a model
    # call blocks, plus margin. max(2I, I + W + 120) per review.
    wait = 600
    ttl = max(2 * interval_minutes * 60, interval_minutes * 60 + wait + 120)
    print("Create a ChatGPT scheduled task with this prompt, repeating every "
          f"{interval_minutes} minutes:\n")
    print(TASK_PROMPT)
    print()
    print("Set the connector permission to \"Allow all actions\": a "
          "scheduled run cannot tap a confirmation card.")
    print()
    print("Requests must outlive the gap to the next run plus the model-call "
          "wait window (TTL = max(2I, I + wait + 120s)):")
    print(f'  export ADVICE_TTL_SECONDS={ttl}   # {ttl // 60} minutes')
    print()
    print("After that the app can stay closed. Nothing local types anywhere; "
          "`codex-remote respond --watch` becomes unnecessary.")
    return 0
