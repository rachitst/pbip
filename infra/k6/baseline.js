// PBIP performance baseline (TASK-012): unauthenticated health endpoints at 50 virtual users.
//
//   python scripts/dev.py perf                 # native k6 if installed, else the grafana/k6 image
//   BASE_URL=http://localhost:8080 python scripts/dev.py perf   # through nginx instead of the API
//
// Pass/fail is decided by the thresholds below: k6 exits non-zero when any threshold is crossed.
import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || "30s";
// Mean think time between iterations. It is randomised (+/-50%) so the 50 VUs do not fire in
// lock-step bursts - a fixed sleep makes every VU hit the server in the same millisecond, which
// measures a synthetic thundering herd rather than steady load. A baseline measures latency under
// steady, realistic load; saturation is the Sprint 6 stress suite's job.
const THINK_SECONDS = Number(__ENV.THINK_SECONDS || 0.5);

export const options = {
  scenarios: {
    // Ramp to 50 VUs, then hold: starting all VUs in the same instant measures a burst, not load.
    health_baseline: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "5s", target: VUS },
        { duration: DURATION, target: VUS },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.001"], // error rate < 0.1%
    http_req_duration: ["p(95)<50"], // p95 < 50 ms across both endpoints
    "http_req_duration{endpoint:live}": ["p(95)<50"],
    "http_req_duration{endpoint:ready}": ["p(95)<50"],
    checks: ["rate>0.999"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  const live = http.get(`${BASE_URL}/api/health/live`, {
    tags: { endpoint: "live" },
  });
  check(live, {
    "live: status 200": (r) => r.status === 200,
    "live: status ok": (r) => r.status === 200 && r.json("status") === "ok",
  });

  const ready = http.get(`${BASE_URL}/api/health/ready`, {
    tags: { endpoint: "ready" },
  });
  check(ready, {
    "ready: status 200": (r) => r.status === 200,
    "ready: db and redis ok": (r) =>
      r.status === 200 &&
      r.json("checks.database") === "ok" &&
      r.json("checks.redis") === "ok",
  });

  sleep(THINK_SECONDS * (0.5 + Math.random()));
}
