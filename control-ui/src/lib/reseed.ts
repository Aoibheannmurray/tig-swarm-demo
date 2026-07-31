// One place to word the outcome of /local-api/swarm/reseed — shown on the
// companion's Host page and in the Admin Console's pools tab, which must not
// drift apart (a `verified: false` reply means the pool could not be read
// back, so an empty `missing` list there proves nothing).
export function formatReseedResult(r: any): string {
  let msg = `Re-seeded ${r.deposited}/${r.total} authored seed(s)`;
  if (r.missing?.length) msg += ` — still missing: ${r.missing.join(", ")}`;
  else if (r.verified === false) msg += " — UNCONFIRMED (could not read the pool back).";
  else msg += " — pool verified.";
  if (r.mainnet) {
    msg += r.mainnet_failed?.length
      ? ` Mainnet: failed for ${r.mainnet_failed.join(", ")}.`
      : " Mainnet algorithm deposited.";
  }
  return msg;
}
