"use strict";

const { spawnSync } = require("node:child_process");

// 同时运行领域场景测试与 HTTP 契约测试
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v", "test_traceability", "service_contract"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
