/**
 * JSONata Expression Tests for SFN Templates
 *
 * Tests critical JSONata expressions from Step Function templates
 * with mock inputs to catch:
 * - Default value fallbacks not working
 * - Missing field access errors
 * - Type conversion bugs
 * - String building errors
 * - Conditional logic bugs
 *
 * Run: cd tests/sfn_jsonata && npm install && npm test
 */

const jsonata = require("jsonata");
const fs = require("fs");
const path = require("path");

let passed = 0;
let failed = 0;
const errors = [];

async function test(name, expr, input, expected, opts = {}) {
  try {
    // In SFN, $states is a reserved variable.
    // In jsonata lib, use assign() to bind it.
    const compiled = jsonata(expr);
    compiled.assign("states", { input, ...opts.extra });
    const result = await compiled.evaluate({});

    // Compare
    const resultStr = JSON.stringify(result);
    const expectedStr = JSON.stringify(expected);

    if (opts.check) {
      if (!opts.check(result)) {
        failed++;
        errors.push(`  ❌ ${name}: custom check failed, got ${resultStr}`);
        return;
      }
    } else if (resultStr !== expectedStr) {
      failed++;
      errors.push(`  ❌ ${name}: expected ${expectedStr}, got ${resultStr}`);
      return;
    }
    passed++;
    console.log(`  ✅ ${name}`);
  } catch (e) {
    failed++;
    errors.push(`  ❌ ${name}: ${e.message}`);
  }
}

// ============================================================
// Extract JSONata expressions from templates for auto-testing
// ============================================================

function extractJsonataExpressions(templatePath) {
  const content = fs.readFileSync(templatePath, "utf8");
  const regex = /\{%\s*([\s\S]*?)\s*%\}/g;
  const exprs = [];
  let match;
  while ((match = regex.exec(content)) !== null) {
    exprs.push(match[1].trim());
  }
  return exprs;
}

function loadTemplate(name) {
  const base = path.join(
    __dirname,
    "..",
    "..",
    "sam",
    "sfn_templates"
  );
  const helperPath = path.join(base, "helpers", name, "sfn.tpl.json");
  const directPath = path.join(base, name, "sfn.tpl.json");
  if (fs.existsSync(helperPath)) return JSON.parse(fs.readFileSync(helperPath, "utf8"));
  if (fs.existsSync(directPath)) return JSON.parse(fs.readFileSync(directPath, "utf8"));
  throw new Error(`Template not found: ${name}`);
}

// ============================================================
// Test Suite: Default Value Fallbacks
// ============================================================

async function testDefaults() {
  console.log("\n📋 Default Value Fallbacks");

  // Registration: trigger_rule defaults to all_success
  await test(
    "trigger_rule defaults to all_success",
    "$exists($states.input.trigger_rule) ? $states.input.trigger_rule : 'all_success'",
    {},
    "all_success"
  );

  await test(
    "trigger_rule uses provided value",
    "$exists($states.input.trigger_rule) ? $states.input.trigger_rule : 'all_success'",
    { trigger_rule: "one_failed" },
    "one_failed"
  );

  // wait_before defaults to 0
  await test(
    "wait_before defaults to 0",
    "$string($exists($states.input.wait_before) ? $states.input.wait_before : 0)",
    {},
    "0"
  );

  await test(
    "wait_before uses provided value",
    "$string($exists($states.input.wait_before) ? $states.input.wait_before : 0)",
    { wait_before: 300 },
    "300"
  );

  // pipeline_name defaults to 'unknown'
  await test(
    "pipeline_name defaults to unknown",
    "$exists($states.input.pipeline_name) ? $states.input.pipeline_name : 'unknown'",
    {},
    "unknown"
  );

  await test(
    "pipeline_name uses provided value",
    "$exists($states.input.pipeline_name) ? $states.input.pipeline_name : 'unknown'",
    { pipeline_name: "my-pipeline" },
    "my-pipeline"
  );

  // task_type defaults to 'sfn'
  await test(
    "task_type defaults to sfn",
    "$exists($states.input.task_type) ? $states.input.task_type : 'sfn'",
    {},
    "sfn"
  );

  // outlets defaults to empty array string
  await test(
    "outlets defaults to empty array",
    "$exists($states.input.outlets) ? $string($states.input.outlets) : '[]'",
    {},
    "[]"
  );

  // wait_for defaults to empty array string
  await test(
    "wait_for defaults to empty array",
    "$exists($states.input.wait_for) ? $string($states.input.wait_for) : '[]'",
    {},
    "[]"
  );

  // orchestration_timeout defaults to 86400
  await test(
    "orchestration_timeout defaults to 86400",
    "$exists($states.input.orchestration_timeout) ? $states.input.orchestration_timeout : 86400",
    {},
    86400
  );

  await test(
    "orchestration_timeout uses provided value",
    "$exists($states.input.orchestration_timeout) ? $states.input.orchestration_timeout : 86400",
    { orchestration_timeout: 259200 },
    259200
  );
}

// ============================================================
// Test Suite: Type Conversions
// ============================================================

async function testTypeConversions() {
  console.log("\n📋 Type Conversions");

  // $string() on numbers — DynamoDB N type requires string
  await test(
    "$string on number for DynamoDB",
    "$string($states.input.ttl)",
    { ttl: 1234567 },
    "1234567"
  );

  await test(
    "$string on dependencies array",
    "$string($states.input.dependencies)",
    { dependencies: ["task_a", "task_b"] },
    '["task_a","task_b"]'
  );

  // Attempt number conversion
  await test(
    "attempt defaults to 1",
    "$string($exists($states.input.attempt) ? $states.input.attempt : 1)",
    {},
    "1"
  );

  await test(
    "attempt uses provided value",
    "$string($exists($states.input.attempt) ? $states.input.attempt : 1)",
    { attempt: 3 },
    "3"
  );
}

// ============================================================
// Test Suite: Conditional Logic
// ============================================================

async function testConditionals() {
  console.log("\n📋 Conditional Logic");

  // Subscriber count check
  await test(
    "subscriber count > 0 with subscribers",
    "$count($states.input.subscribers) > 0",
    { subscribers: [{ name: "task_b" }] },
    true
  );

  await test(
    "subscriber count > 0 without subscribers",
    "$count($states.input.subscribers) > 0",
    { subscribers: [] },
    false
  );

  // Pause check
  await test(
    "pause check - paused true",
    "$exists($states.input._pause_check.paused.BOOL) and $states.input._pause_check.paused.BOOL = true",
    { _pause_check: { paused: { BOOL: true } } },
    true
  );

  await test(
    "pause check - not paused",
    "$exists($states.input._pause_check.paused.BOOL) and $states.input._pause_check.paused.BOOL = true",
    { _pause_check: { paused: { BOOL: false } } },
    false
  );

  await test(
    "pause check - no pause field",
    "$exists($states.input._pause_check.paused.BOOL) and $states.input._pause_check.paused.BOOL = true",
    { _pause_check: {} },
    false
  );

  // Has dependencies check
  await test(
    "has dependencies - with deps",
    "$states.input.dependencies != '[]' and $states.input.dependencies != ''",
    { dependencies: '["task_a"]' },
    true
  );

  await test(
    "has dependencies - empty",
    "$states.input.dependencies != '[]' and $states.input.dependencies != ''",
    { dependencies: "[]" },
    false
  );
}

// ============================================================
// Test Suite: String Building (Slack/PagerDuty messages)
// ============================================================

async function testStringBuilding() {
  console.log("\n📋 String Building");

  // Slack message formatting
  await test(
    "Slack pipeline label",
    "'*Pipeline:* `' & $states.input.pipeline_name & '`'",
    { pipeline_name: "etl-pipeline" },
    "*Pipeline:* `etl-pipeline`"
  );

  await test(
    "Slack task label",
    "'*Task:* `' & $states.input.task_name & '`'",
    { task_name: "extract_data" },
    "*Task:* `extract_data`"
  );

  // Execution name truncation
  await test(
    "substring truncation for SFN name",
    "$substring('restart-' & $string($millis()) & '-' & $states.input.task_name, 0, 80)",
    { task_name: "my_task" },
    null,
    {
      check: (r) =>
        r.startsWith("restart-") && r.includes("my_task") && r.length <= 80,
    }
  );
}

// ============================================================
// Test Suite: Error Handling Expressions
// ============================================================

async function testErrorHandling() {
  console.log("\n📋 Error Handling");

  // Error truncation
  await test(
    "error truncation - short error passes through",
    "( $err := $string($states.input.error); $length($err) > 50000 ? $substring($err, 0, 50000) & '...[truncated]' : $err )",
    { error: "Task timed out" },
    "Task timed out"
  );

  await test(
    "error truncation - long error gets truncated",
    "( $err := $string($states.input.error); $length($err) > 100 ? $substring($err, 0, 100) & '...[truncated]' : $err )",
    { error: "x".repeat(200) },
    "x".repeat(100) + "...[truncated]"
  );

  // pipeline_execution_short fallback
  await test(
    "pipeline_execution_short fallback - has value",
    "( $short := $exists($states.input.pipeline_execution_short) and $states.input.pipeline_execution_short != '' ? $states.input.pipeline_execution_short : 'unknown'; $short )",
    { pipeline_execution_short: "abc123" },
    "abc123"
  );

  await test(
    "pipeline_execution_short fallback - empty string",
    "( $short := $exists($states.input.pipeline_execution_short) and $states.input.pipeline_execution_short != '' ? $states.input.pipeline_execution_short : 'unknown'; $short )",
    { pipeline_execution_short: "" },
    "unknown"
  );

  await test(
    "pipeline_execution_short fallback - missing",
    "( $short := $exists($states.input.pipeline_execution_short) and $states.input.pipeline_execution_short != '' ? $states.input.pipeline_execution_short : 'unknown'; $short )",
    {},
    "unknown"
  );
}

// ============================================================
// Test Suite: Transform Expressions
// ============================================================

async function testTransforms() {
  console.log("\n📋 Transform Expressions");

  // Input merging with |$| operator
  await test(
    "input merge - add error field",
    "$states.input ~> |$| {'error': 'something broke'} |",
    { task_name: "test", execution_name: "exec-1" },
    { task_name: "test", execution_name: "exec-1", error: "something broke" }
  );

  await test(
    "input merge - add pause_timeout",
    "$states.input ~> |$| {'pause_timeout': true} |",
    { task_name: "test" },
    { task_name: "test", pause_timeout: true }
  );

  await test(
    "input merge - is_restart flag",
    "$states.input ~> |$| {'is_restart': true} |",
    { task_name: "test", date: "2026-01-19" },
    { task_name: "test", date: "2026-01-19", is_restart: true }
  );

  console.log("\n📋 pull() context injection");
  // Lambda payload carries pipeline_name + date + table so pull("A", event) can build the key.
  await test(
    "Lambda payload injects pipeline_name + date + _polyris_table (+ keeps user payload)",
    "$merge([$exists($states.input.task_config.payload) ? $states.input.task_config.payload : {}, {'current_date': $states.input.current_date, 'PARTITION_ARG': $states.input.PARTITION_ARG, 'pipeline_name': $states.input.pipeline_name, 'date': $states.input.date, '_polyris_table': 'tok-tbl'}, $exists($states.input.variables) ? $states.input.variables : {}, $exists($states.input.upstream) and $count($keys($states.input.upstream)) > 0 ? {'upstream': $states.input.upstream} : {}])",
    { pipeline_name: "sales", date: "2026-07-07", current_date: "2026-07-07", PARTITION_ARG: "2026-07-07", task_config: { payload: { custom: 1 } } },
    null,
    { check: (r) => r.pipeline_name === "sales" && r.date === "2026-07-07" && r._polyris_table === "tok-tbl" && r.custom === 1 }
  );

  // Glue job arguments carry the same context as --POLYRIS_* flags (date = store key field).
  await test(
    "Glue Arguments inject --POLYRIS_* context (+ keep user args)",
    "$merge([$exists($states.input.task_config.arguments) ? $states.input.task_config.arguments : {}, {'--POLYRIS_PIPELINE_NAME': $states.input.pipeline_name, '--POLYRIS_RUN_DATE': $states.input.date, '--POLYRIS_TOKENS_TABLE': 'tok-tbl'}])",
    { pipeline_name: "sales", date: "2026-07-07", task_config: { arguments: { "--src": "s3://x" } } },
    null,
    { check: (r) => r["--POLYRIS_PIPELINE_NAME"] === "sales" && r["--POLYRIS_RUN_DATE"] === "2026-07-07" && r["--POLYRIS_TOKENS_TABLE"] === "tok-tbl" && r["--src"] === "s3://x" }
  );

  // ECS: POLYRIS_* env appended into each ContainerOverride's Environment (PascalCase).
  await test(
    "ECS Overrides inject POLYRIS_* env, preserving user env + array shape",
    "($ov := $exists($states.input.task_config.overrides) ? $states.input.task_config.overrides : {}; $penv := [{'Name': 'POLYRIS_PIPELINE_NAME', 'Value': $states.input.pipeline_name}, {'Name': 'POLYRIS_RUN_DATE', 'Value': $states.input.date}, {'Name': 'POLYRIS_TOKENS_TABLE', 'Value': 'tok-tbl'}]; $exists($ov.ContainerOverrides) ? $merge([$ov, {'ContainerOverrides': [$map($ov.ContainerOverrides, function($co) { $merge([$co, {'Environment': $append($exists($co.Environment) ? $co.Environment : [], $penv)}]) })]}]) : $ov)",
    { pipeline_name: "sales", date: "2026-07-07", task_config: { overrides: { ContainerOverrides: [{ Name: "app", Environment: [{ Name: "USER_VAR", Value: "x" }] }] } } },
    null,
    { check: (r) => { const e = r.ContainerOverrides[0].Environment; return e.length === 4 && e[0].Name === "USER_VAR" && e[1].Name === "POLYRIS_PIPELINE_NAME" && e[1].Value === "sales" && e[2].Value === "2026-07-07" && e[3].Name === "POLYRIS_TOKENS_TABLE"; } }
  );

  // Batch: POLYRIS_* env via ContainerOverrides.Environment (single container).
  await test(
    "Batch ContainerOverrides carry POLYRIS_* env",
    "{'Environment': [{'Name': 'POLYRIS_PIPELINE_NAME', 'Value': $states.input.pipeline_name}, {'Name': 'POLYRIS_RUN_DATE', 'Value': $states.input.date}, {'Name': 'POLYRIS_TOKENS_TABLE', 'Value': 'tok-tbl'}]}",
    { pipeline_name: "sales", date: "2026-07-07" },
    null,
    { check: (r) => r.Environment.length === 3 && r.Environment[0].Value === "sales" && r.Environment[1].Value === "2026-07-07" }
  );
}

// ============================================================
// Test Suite: Auto-extracted expression smoke test
// ============================================================

async function testTemplateExpressionsCompile() {
  console.log("\n📋 Template Expression Compilation (smoke test)");

  const templatesDir = path.join(
    __dirname,
    "..",
    "..",
    "sam",
    "sfn_templates"
  );

  let totalExprs = 0;
  let compileErrors = 0;
  const templateErrors = {};

  function scanDir(dir) {
    const files = fs.readdirSync(dir, { withFileTypes: true });
    for (const file of files) {
      const fullPath = path.join(dir, file.name);
      if (file.isDirectory()) {
        scanDir(fullPath);
      } else if (file.name.endsWith(".json")) {
        const content = fs.readFileSync(fullPath, "utf8");
        const regex = /\{%\s*([\s\S]*?)\s*%\}/g;
        let match;
        while ((match = regex.exec(content)) !== null) {
          totalExprs++;
          const expr = match[1].trim();
          // Skip expressions with Terraform ${} vars — they aren't valid JSONata
          if (expr.includes("${")) continue;
          try {
            jsonata(expr);
          } catch (e) {
            compileErrors++;
            const relPath = path.relative(templatesDir, fullPath);
            if (!templateErrors[relPath]) templateErrors[relPath] = [];
            templateErrors[relPath].push({
              expr: expr.substring(0, 80),
              error: e.message,
            });
          }
        }
      }
    }
  }

  scanDir(templatesDir);

  if (compileErrors > 0) {
    failed++;
    const details = Object.entries(templateErrors)
      .map(([file, errs]) =>
        `  ${file}:\n${errs.map((e) => `    "${e.expr}..." → ${e.error}`).join("\n")}`
      )
      .join("\n");
    errors.push(`  ❌ JSONata compilation: ${compileErrors}/${totalExprs} expressions failed:\n${details}`);
  } else {
    passed++;
    console.log(`  ✅ All ${totalExprs} JSONata expressions compile successfully`);
  }
}

// ============================================================
// Runner
// ============================================================

async function main() {
  console.log("🧪 SFN JSONata Expression Tests\n");

  await testDefaults();
  await testTypeConversions();
  await testConditionals();
  await testStringBuilding();
  await testErrorHandling();
  await testTransforms();
  await testTemplateExpressionsCompile();

  
await test("task_input captures upstream + variables", `( $ti := $string({'upstream': $exists($states.input.upstream) ? $states.input.upstream : {}, 'variables': $exists($states.input.variables) ? $states.input.variables : {}}); $length($ti) > 25000 ? $string({'variables': $exists($states.input.variables) ? $states.input.variables : {}, '_upstream_omitted': true, '_size': $length($ti)}) : $ti )`,
  { upstream: { a: { output: { n: 1 } } }, variables: { year: "2026" } },
  null,
  { check: (r) => { const o = JSON.parse(r); return o.upstream.a.output.n === 1 && o.variables.year === "2026"; } });

await test("task_input omits large upstream, keeps variables", `( $ti := $string({'upstream': $exists($states.input.upstream) ? $states.input.upstream : {}, 'variables': $exists($states.input.variables) ? $states.input.variables : {}}); $length($ti) > 25000 ? $string({'variables': $exists($states.input.variables) ? $states.input.variables : {}, '_upstream_omitted': true, '_size': $length($ti)}) : $ti )`,
  { upstream: { big: { output: "x".repeat(30000) } }, variables: { year: "2026" } },
  null,
  { check: (r) => { const o = JSON.parse(r); return o._upstream_omitted === true && o.variables.year === "2026"; } });

console.log(`\n${"=".repeat(50)}`);
  console.log(`Results: ${passed} passed, ${failed} failed`);
  console.log("=".repeat(50));

  if (errors.length > 0) {
    console.log("\nFailures:");
    errors.forEach((e) => console.log(e));
  }

  process.exit(failed > 0 ? 1 : 0);
}

main().catch((e) => {
  console.error("Fatal error:", e);
  process.exit(1);
});
