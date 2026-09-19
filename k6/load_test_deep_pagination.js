import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

const counterPerLimit = new Counter("limit_counter");

export const options = {
  scenarios: {
    ramping: {
      executor: "constant-vus",
      vus: 10,
      duration: "90s",
    },
  },
  thresholds: {
    "limit_counter{limit:100000}": ["count>=0"],
    "limit_counter{limit:300000}": ["count>=0"],
    "limit_counter{limit:600000}": ["count>=0"],
    "limit_counter{limit:800000}": ["count>=0"],
    "limit_counter{limit:1000000}": ["count>=0"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const LIMITS = [
  { limit: 100_000, p: 0.05 },
  { limit: 300_000, p: 0.05 },
  { limit: 600_000, p: 0.05 },
  { limit: 800_000, p: 0.7 },
  { limit: 1_000_000, p: 0.15 },
];
const MAX_ID = 50_000_000; // matches the seeded row count

export default function () {
  const random = Math.random();
  let chosenLimitValue = 0;
  let runningTotal = 0;
  for (const limit of LIMITS) {
    runningTotal += limit.p;
    if (runningTotal > random) {
      chosenLimitValue = limit.limit;
      counterPerLimit.add(1, { limit: limit.limit });
      break;
    }
  }

  const res = http.get(`${BASE_URL}/feed/full-scan?limit=${chosenLimitValue}`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.1);
}
