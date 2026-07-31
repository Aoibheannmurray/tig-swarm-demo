<script lang="ts">
  export let steps: string[];
  export let current: number;
</script>

<ol class="stepper">
  {#each steps as label, i}
    <li class:done={i < current} class:active={i === current}>
      <span class="dot">{i < current ? "✓" : i + 1}</span>
      <span class="label">{label}</span>
    </li>
  {/each}
</ol>

<style>
  .stepper {
    display: flex;
    list-style: none;
    gap: 6px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }
  li {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    color: var(--ink-faint);
    padding-right: 10px;
  }
  li .dot {
    width: 22px;
    height: 22px;
    border-radius: 999px;
    display: grid;
    place-items: center;
    font-size: 11px;
    font-weight: 700;
    background: var(--bg-page);
    border: 1px solid var(--border-default);
    color: var(--ink-dim);
  }
  li.active { color: var(--ink); }
  li.active .dot {
    background: var(--color-accent);
    border-color: var(--color-accent);
    color: #fff;
  }
  li.done { color: var(--ink-mid); }
  li.done .dot {
    background: var(--success-bg);
    border-color: var(--success);
    color: var(--success);
  }
</style>
