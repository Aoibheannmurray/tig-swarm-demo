<script lang="ts">
  // Colorized, auto-scrolling log viewer. `lines` is an array of {name?, line}.
  export let lines: { name?: string; line: string }[] = [];
  export let height = "320px";

  let box: HTMLDivElement | undefined;
  let pinned = true;

  // A stable earthen color per agent name (mirrors the dashboard's palette).
  const PALETTE = [
    "#B8541F", "#C68F3E", "#6B7F4E", "#4E6B85",
    "#7A4F6E", "#4A8C8A", "#A66E45", "#8B6B8C",
  ];
  function colorFor(name: string | undefined): string {
    if (!name) return "var(--ink-dim)";
    let h = 2166136261;
    for (let i = 0; i < name.length; i++) {
      h ^= name.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return PALETTE[Math.abs(h) % PALETTE.length];
  }

  $: if (lines && box && pinned) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));

  function onScroll() {
    if (!box) return;
    pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  }
</script>

<div class="logbox" bind:this={box} on:scroll={onScroll} style="height:{height}">
  {#if lines.length === 0}
    <div class="empty">Waiting for output…</div>
  {/if}
  {#each lines as l}
    <div class="line">
      {#if l.name}<span class="tag" style="color:{colorFor(l.name)}">{l.name}</span>{/if}
      <span class="txt">{l.line}</span>
    </div>
  {/each}
</div>

<style>
  .logbox {
    background: #201d1a;
    color: #e7e0d6;
    font-family: var(--mono);
    font-size: 12.5px;
    line-height: 1.55;
    border-radius: 6px;
    padding: 12px 14px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .empty { color: rgba(231, 224, 214, 0.4); }
  .line { display: flex; gap: 8px; }
  .tag { flex: none; font-weight: 700; }
  .txt { flex: 1; }
</style>
