// load_test.js
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    ramping: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 50 },
        { duration: "30s", target: 200 },
        { duration: "30s", target: 500 },
        { duration: "30s", target: 800 },
        { duration: "30s", target: 1000 },
      ],
    },
  },
  thresholds: {
    http_req_duration: ["p(99)<200"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const REGIONS = ["US", "EU", "APAC", "LATAM"];

export default function () {
  const region = REGIONS[Math.floor(Math.random() * REGIONS.length)];
  const res = http.get(`${BASE_URL}/feed?region=${region}&limit=20`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.1);
}
