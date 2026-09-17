// load_test.js
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

const counterPerRegion = new Counter("region_counter");
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
    "region_counter{region:US}": ["count>=0"],
    "region_counter{region:EU}": ["count>=0"],
    "region_counter{region:APAC}": ["count>=0"],
    "region_counter{region:LATAM}": ["count>=0"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const REGIONS = [
  { region: "US", value: 0.7 },
  { region: "EU", value: 0.2 },
  { region: "APAC", value: 0.07 },
  { region: "LATAM", value: 0.03 },
];
export default function () {
  const randomNo = Math.random();
  let chosenRegion = "";
  let runningTotal = 0;
  for (const region of REGIONS) {
    runningTotal += region.value;
    if (runningTotal > randomNo) {
      chosenRegion = region.region;
      counterPerRegion.add(1, { region: region.region });
      break;
    }
  }
  const res = http.get(`${BASE_URL}/feed?region=${chosenRegion}&limit=20`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.1);
}
