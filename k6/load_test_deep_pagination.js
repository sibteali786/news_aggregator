import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    ramping: {
      executor: "constant-vus",
      vus: 10,
      duration: "90s",
    },
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const LIMITS = [100_000, 300_000, 600_000, 800_000, 1_000_000];
const MAX_ID = 50_000_000; // matches the seeded row count

export default function () {
  const limit = LIMITS[Math.floor(Math.random() * LIMITS.length)];

  const res = http.get(`${BASE_URL}/feed/full-scan?limit=${limit}`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.1);
}
