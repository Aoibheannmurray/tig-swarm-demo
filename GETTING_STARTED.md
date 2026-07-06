# Getting Started — Join the GPU Swarm

This is a **GPU swarm**: your agents write Rust solvers for a TIG challenge and
benchmark them on GPUs. We've sourced the GPU compute for everyone from
[C3 Cloud (Cambridge Compute Co)](https://cthree.cloud), so you don't need a
local GPU — every benchmark runs remotely.

Three steps: **clone the repo**, **set up C3**, then **run the wizard**. Budget
about 10 minutes.

> **Windows:** use `python` (not `python3`) in every command below, and use
> `set` / `$env:` instead of `export` (the exact lines are shown where they
> matter). For installing the `c3` CLI on Windows, see the [README](./README.md).

---

## Step 1 — Clone the repo and check requirements

```bash
git clone https://github.com/Aoibheannmurray/tig-swarm-demo.git
cd tig-swarm-demo
```

You need:

- **Python 3**
- **Git** — each agent runs in its own git worktree
- **Either** an API key for your chosen LLM provider (Anthropic, OpenAI, Google,
  …) **or** a logged-in `claude` / `codex` CLI

You do **not** need Docker for this swarm — all benchmarks run on C3.

---

## Step 2 — Set up C3 (GPU compute)

Install the `c3` CLI (Windows users: see the [README](./README.md) instead):

```bash
curl -fsSL https://cthree.cloud/install.sh | sh
```

Log in (you'll be prompted to create an account — do so, then return to the
terminal):

```bash
c3 login
```

Confirm you're logged in and create an API key:

```bash
c3 whoami                       # should print your account — confirms login
c3 apikey create tig-swarm      # creates your API key
```

**Save the key somewhere safe** — it starts with `c3_...`. You'll export it in
the next step.

---

## Step 3 — Export your keys *before* launching

This is the step people forget. Export both keys in the **same terminal** you'll
run the wizard from, so the fleet can authenticate without you pasting anything:

```bash
# macOS / Linux
export C3_API_KEY=c3_...                 # the key from Step 2
export ANTHROPIC_API_KEY=sk-...          # your provider key (skip if using a CLI login)
```

```powershell
# Windows PowerShell
$env:C3_API_KEY="c3_..."
$env:ANTHROPIC_API_KEY="sk-..."
# Windows cmd.exe (no quotes):  set C3_API_KEY=c3_...
```

Using the `claude` or `codex` CLI instead of an API key? You don't need a
provider key — just make sure you're logged in (`claude login` / `codex login`).
You still need `C3_API_KEY` for the GPU compute.

> **Forgot to export?** No problem. Finish the wizard anyway (you can paste the
> C3 key when it asks), or export the key now and just **re-run the wizard** —
> your previous answers come back as the defaults, so you can press **Enter**
> straight through.

---

## Step 4 — Run the wizard and launch

```bash
python3 run.py
```

This walks you through a short config wizard, then launches your fleet. At every
prompt, the default is shown in `[brackets]` — **press Enter to accept it**.

What it asks:

1. **Swarm connection** — paste the `server_url` / `username` / `swarm_password`
   your swarm host sent you (hosts generate these with `python3 setup.py invite`
   — see the [README's Host section](./README.md#host)). On a re-run these are
   remembered — just press Enter.
2. **LLM provider & model** — pick your provider; press Enter for the default
   model.
3. **How many agents** — start with `1` if you're unsure.
4. **Where should each benchmark run?** — choose **C3 cloud GPU** (the default).
5. **C3 API key** — if you exported `C3_API_KEY` in Step 3, the wizard detects
   it and there's nothing to paste. If not, paste the `c3_...` key here.

That's it — the fleet starts and you watch progress via the dashboard URL it
prints. `Ctrl-C` stops the whole fleet; your agents persist across restarts.

---

## Re-running later

Just run `python3 run.py` again. If you already have a `fleet.config.json`, it
skips setup and relaunches (with a couple of optional "update?" prompts you can
skip with Enter). To change provider, model, or agent count, answer **y** to the
update prompt and walk the wizard again — Enter reuses every previous answer.

For hand-editing config, tacit-knowledge hints, and power-user commands, see the
[README](./README.md#contributor).
