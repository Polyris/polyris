/**
 * Glue/Hive DDL renderer — TypeScript mirror of `polyris.assets._render_glue_ddl`.
 *
 * Why this exists: the UI bundle ships independently of the Python SDK and
 * cannot import Python at runtime. To keep the "Copy as DDL" button instant
 * (no network round-trip), we mirror the SDK's renderer here. The two
 * implementations are kept in lockstep by a shared fixture file
 * (`tests/fixtures/ddl_parity.json`) and parity tests on both sides:
 *
 *   - `tests/sdk/test_ddl_parity.py`        — pytest checks Python output
 *   - `src/utils/__tests__/ddl-glue-parity.test.ts` — vitest checks TS output
 *
 * Both tests load the same JSON fixtures; either side drifting is a CI fail.
 *
 * When a second dialect lands (BigQuery, Iceberg, Postgres, Snowflake, ...),
 * this file gets a sibling — see ADR #46 for the planned plugin pattern.
 * Until then, one file, one function — no abstraction tax.
 */
import type { AssetSchemaColumn } from '@/types';


export interface RenderGlueDDLInput {
    /** Asset name — used as a fallback table identifier when `glueTable` is empty. */
    assetName: string;
    /** Glue table reference in `database.table` form. Takes precedence over `assetName`. */
    glueTable: string;
    /** Asset description — emitted as a top-level `COMMENT '...'` clause when present. */
    description: string;
    /** Asset URI — emitted as `LOCATION '...'` when it starts with `s3://` or `s3a://`. */
    uri: string;
    /** Column list. Columns with `partition_key: true` are extracted into `PARTITIONED BY`. */
    schema: AssetSchemaColumn[];
}


/**
 * Render a Glue/Hive `CREATE EXTERNAL TABLE` DDL statement.
 *
 * The output is byte-identical to `Asset.to_ddl(dialect='glue')` from the
 * Python SDK — single-quote doubling, partition extraction, S3 URI handling,
 * backtick quoting around the table identifier when no `glueTable` is set.
 *
 * Throws on empty schema (matches Python `ValueError`). The caller should
 * gate the button so this is reached only when there is something to render.
 */
export function renderGlueDDL(input: RenderGlueDDLInput): string {
    const { assetName, glueTable, description, uri, schema } = input;

    if (!schema || schema.length === 0) {
        // Mirror the Python ValueError message so test fixtures cover both sides.
        throw new Error(
            `renderGlueDDL: asset ${JSON.stringify(assetName)} has no schema declared — ` +
            `nothing to render. Add columns via Asset(..., schema=[...]) ` +
            `or one of the from_* constructors.`
        );
    }

    // Glue tables look like `database.table`; bare asset names go in
    // backticks so non-identifier characters survive.
    const tableId = glueTable || `\`${assetName}\``;
    const escape = (s: string) => s.replace(/'/g, "''");

    const regular: string[] = [];
    const partitions: string[] = [];
    for (const col of schema) {
        let line = `  \`${col.name}\` ${col.type}`;
        if (col.description) {
            line += ` COMMENT '${escape(col.description)}'`;
        }
        (col.partition_key ? partitions : regular).push(line);
    }

    const lines: string[] = [
        `CREATE EXTERNAL TABLE ${tableId} (`,
        regular.join(',\n'),
        ')',
    ];
    if (partitions.length > 0) {
        lines.push('PARTITIONED BY (', partitions.join(',\n'), ')');
    }
    if (description) {
        lines.push(`COMMENT '${escape(description)}'`);
    }
    if (uri && (uri.startsWith('s3://') || uri.startsWith('s3a://'))) {
        lines.push(`LOCATION '${uri}'`);
    }
    return lines.join('\n');
}
