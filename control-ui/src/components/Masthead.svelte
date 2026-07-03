<script lang="ts">
  export let title: string;
  export let subtitle: string = "";
</script>

<header class="masthead">
  <img src="/prometheus-icon.png" alt="Prometheus" />
  <div>
    <div class="title">{title}</div>
    {#if subtitle}<div class="subtitle">{subtitle}</div>{/if}
  </div>
  <div class="spacer"></div>
  <slot />
</header>
