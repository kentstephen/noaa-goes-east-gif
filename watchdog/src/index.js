// Starts the daily GitHub Actions render when GitHub's own schedule does not.
//
// GitHub's schedule can be late, skip a day, or never start. A few times each
// morning this Worker reads the `evening` metadata on the current hero loops in R2.
// If either is older than last evening and no render is already queued or running,
// it starts the workflow through the GitHub API (a manual dispatch, which GitHub
// runs right away).
//
// It also re-enables the workflow if GitHub has turned it off (the 60-day
// inactivity rule), using the owner's token, so dispatches keep working.

const REPO = "kentstephen/noaa-goes-east-gif";
const WORKFLOW = "hero.yml";
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

async function check(env) {
  // Every run calls GitHub at least once, so the log shows the token still works.
  const wf = await (await github(env, `/actions/workflows/${WORKFLOW}`)).json();
  let enabled = "";
  if (wf.state !== "active") {
    await github(env, `/actions/workflows/${WORKFLOW}/enable`, { method: "PUT" });
    enabled = `re-enabled (was ${wf.state}); `;
  }

  const want = expectedEvening(new Date());
  const have = {};
  for (const key of LOOPS) {
    const head = await env.BUCKET.head(key);
    have[key] = head?.customMetadata?.evening ?? "missing";
  }
  const stale = LOOPS.filter((k) => have[k] < want || have[k] === "missing");
  if (stale.length === 0) return { want, have, action: enabled + "none, loops are current" };

  for (const status of ["queued", "in_progress"]) {
    const res = await github(env, `/actions/workflows/${WORKFLOW}/runs?status=${status}&per_page=1`);
    if ((await res.json()).total_count > 0) return { want, have, action: enabled + `none, a run is ${status}` };
  }
  await github(env, `/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
  });
  return { want, have, action: enabled + "dispatched a render" };
}

export default {
  async scheduled(event, env, ctx) {
    console.log(JSON.stringify({ cron: event.cron, ...(await check(env)) }));
  },
};
