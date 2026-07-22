<script lang="ts">
  // A shell command (or any block of text) with a copy button. Replaces four
  // ad-hoc copies of this pattern across join/, companion/ and admin/ — one of
  // which had no "copied" feedback at all, so the button looked inert.
  let {
    text,
    label = "Copy",
    variant = "primary",
    multiline = false,
  }: {
    text: string;
    label?: string;
    variant?: "primary" | "ghost";
    multiline?: boolean;
  } = $props();

  let copied = $state(false);
  let failed = $state(false);
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function copy() {
    failed = false;
    try {
      // clipboard is undefined on a plain-HTTP, non-localhost origin. The
      // companion is localhost and /join is HTTPS, so this is the rare case —
      // but a silently dead button is worse than an honest one.
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {
      failed = true;
    }
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { copied = false; failed = false; }, 1800);
  }
</script>

<div class="cmd mono" class:multiline>{text}</div>
<button class={variant} onclick={copy}>
  {failed ? "Press ⌘/Ctrl+C" : copied ? "Copied ✓" : label}
</button>

<style>
  .cmd {
    background: var(--bg-sunken, rgba(127, 127, 127, 0.12));
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
    overflow-x: auto;
    font-size: 0.92em;
    word-break: break-all;
  }
  /* Multi-line blocks (the Windows PowerShell C3 install) keep their newlines
     and scroll rather than wrapping mid-token. */
  .multiline { white-space: pre; word-break: normal; }
</style>
