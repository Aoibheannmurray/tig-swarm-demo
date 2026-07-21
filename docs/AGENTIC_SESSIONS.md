# Inspecting agentic sessions

This optional debugging guide applies to agents using `claude-code-agentic`.

Claude Code records session events for each agent worktree. Use the session
viewer to inspect the prompts, responses, and tool activity that were recorded:

```bash
python3 scripts/show_agent_session.py <agent> --list     # list sessions, newest first
python3 scripts/show_agent_session.py <agent>            # show the newest session
python3 scripts/show_agent_session.py <agent> --index 3  # show an older session
python3 scripts/show_agent_session.py <agent> --full     # do not truncate long blocks
```

Depending on the available log data, the rendered session can include:

- The swarm's current agent instructions from the agent worktree.
- The per-iteration prompt, including the score, role, and task.
- Recorded assistant responses or reasoning summaries.
- Tool calls and their recorded results.

The swarm instructions displayed by the viewer come from the current file in
the worktree and may differ from the instructions used by an older session.
Provider-level system prompts and private internal reasoning are not available
from these logs.
