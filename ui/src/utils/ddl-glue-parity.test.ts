/**
 * DDL parity test — TypeScript side (ADR #46).
 *
 * Reads the same fixture file that the Python parity test uses
 * (`tests/fixtures/ddl_parity.json` at the repo root) and asserts that
 * `renderGlueDDL` produces byte-identical output to each fixture's
 * `expected` string.
 *
 * The fixture is loaded via `fs.readFileSync` rather than a TS `import`
 * because the file lives outside the `ui/` package — sharing one fixture
 * between Python and TS is the whole point. Vitest runs in a Node
 * environment so fs is available; only the path-resolution comment is
 * worth flagging:
 *
 *   process.cwd() at vitest run time is the `ui/` directory, so the
 *   fixture sits one level up at `../tests/fixtures/ddl_parity.json`.
 *
 * If either renderer drifts from the fixture, exactly one of the two
 * parity tests fails — pinpointing which side moved.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

import { renderGlueDDL, type RenderGlueDDLInput } from './ddl-glue';

interface DDLParityFixture {
    name: string;
    description_text: string;
    input: RenderGlueDDLInput;
    expected: string;
}

// `process.cwd()` is the `ui/` directory under `npm test` / `vitest run`.
// Repo root is one level up; the fixture lives under tests/fixtures/.
const FIXTURE_PATH = resolve(process.cwd(), '..', 'tests', 'fixtures', 'ddl_parity.json');

const fixtures: DDLParityFixture[] = JSON.parse(
    readFileSync(FIXTURE_PATH, 'utf-8')
);


describe('renderGlueDDL — fixture parity with Python _render_glue_ddl', () => {
    // Sanity: fixtures actually loaded. Catches a wrong path or empty file
    // before the parametrized cases would silently produce 0 assertions.
    it('loaded fixtures from the shared file', () => {
        expect(fixtures.length).toBeGreaterThan(0);
        // Each fixture must have the four shape keys the loader expects.
        for (const f of fixtures) {
            expect(f.name).toBeTruthy();
            expect(f.input).toBeDefined();
            expect(typeof f.expected).toBe('string');
        }
    });

    // One test per fixture so a failure names the specific case that drifted.
    for (const fixture of fixtures) {
        it(`${fixture.name}: ${fixture.description_text}`, () => {
            const actual = renderGlueDDL(fixture.input);
            expect(actual).toBe(fixture.expected);
        });
    }
});


describe('renderGlueDDL — error cases', () => {
    it('throws on empty schema (matches Python ValueError)', () => {
        expect(() =>
            renderGlueDDL({
                assetName: 'x',
                glueTable: '',
                description: '',
                uri: '',
                schema: [],
            })
        ).toThrow(/has no schema declared/);
    });
});
