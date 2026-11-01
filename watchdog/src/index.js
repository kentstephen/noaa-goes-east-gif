// Starts GitHub Actions jobs when GitHub's own schedule does not.
//
// GitHub's schedule can be late, skip a day, or never start. A few times a day this
// Worker checks two jobs and starts each one through the GitHub API (a manual
// dispatch, which GitHub runs right away) only if it is due:
//
// - Daily render (hero.yml): due when either hero loop in R2 has an `evening` older
//   than last evening and no render is already queued or running.
// - Monthly dependency upgrade (deps.yml): due when it has no run at all this month
//   (UTC). Checked only from 15:00 UTC on, clear of the daily render. A failed
//   upgrade still counts as this month's run, so it is not retried.
//
// It also re-enables either workflow if GitHub has turned it off (the 60-day
// inactivity rule), using the owner's token, so dispatches keep working.

const REPO = "kentstephen/noaa-goes-east-gif";
const RENDER = "hero.yml";
const UPGRADE = "deps.yml";
const LOOPS = ["hero-desktop.mp4", "hero-mobile.mp4"];

// Eastern date of the evening the morning's render should show: yesterday in New York.
function expectedEvening(now) {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(now);
  const [y, m, d] = today.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10);
}

async function github(env, path, init = {}) {
  const res = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "hero-watchdog",
      ...(init.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`GitHub ${path}: ${res.status} ${await res.text()}`);
  return res;
}

// Re-enables a workflow GitHub has turned off. Returns a note for the log.
async function ensureEnabled(env, workflow) {
  const wf = await (await github(env, `/actions/workflows/${workflow}`)).json();
  if (wf.state === "active") return "";
  await github(env, `/actions/workflows/${workflow}/enable`, { method: "PUT" });
  return `re-enabled (was ${wf.state}); `;
}

async function dispatch(env, workflow) {
  await github(env, `/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
  });
}

async function checkRender(env, now) {
  // Every run calls GitHub at least once, so the log shows the token still works.
  const enabled = await ensureEnabled(env, RENDER);

  const want = expectedEvening(now);
  const have = {};
  for (const key of LOOPS) {
    const head = await env.BUCKET.head(key);
    have[key] = head?.customMetadata?.evening ?? "missing";
  }
  const stale = LOOPS.filter((k) => have[k] < want || have[k] === "missing");
  if (stale.length === 0) return { want, have, action: enabled + "none, loops are current" };

  for (const status of ["queued", "in_progress"]) {
    const res = await github(env, `/actions/workflows/${RENDER}/runs?status=${status}&per_page=1`);
    if ((await res.json()).total_count > 0) return { want, have, action: enabled + `none, a run is ${status}` };
  }
  await dispatch(env, RENDER);
  return { want, have, action: enabled + "dispatched a render" };
}

async function checkUpgrade(env, now) {
  if (now.getUTCHours() < 15) return { action: "none, checked from 15:00 UTC" };
  const enabled = await ensureEnabled(env, UPGRADE);

  // Any run created this month, whatever its status (queued, running, done, failed).
  const month = now.toISOString().slice(0, 7);
  const created = encodeURIComponent(`>=${month}-01`);
  const res = await github(env, `/actions/workflows/${UPGRADE}/runs?created=${created}&per_page=1`);
  const runs = await res.json();
  if (runs.total_count > 0) {
    const run = runs.workflow_runs[0];
    const state = run.conclusion ?? run.status;
    return { month, action: enabled + `none, ran ${run.created_at} (${run.event}, ${state})` };
  }
  await dispatch(env, UPGRADE);
  return { month, action: enabled + "dispatched an upgrade" };
}

// Runs one check; an error is logged instead of stopping the other check.
async function attempt(fn, env, now) {
  try {
    return await fn(env, now);
  } catch (err) {
    return { action: "error", error: String(err) };
  }
}

export default {
  async scheduled(event, env, ctx) {
    const now = new Date();
    const render = await attempt(checkRender, env, now);
    const upgrade = await attempt(checkUpgrade, env, now);
    console.log(JSON.stringify({ cron: event.cron, render, upgrade }));
    // Mark the invocation failed in the Cloudflare logs when either check errored.
    if (render.action === "error" || upgrade.action === "error") throw new Error("a check failed");
  },
};
