/**
 * Resolve a Playwright installation without assuming where this checkout lives.
 *
 * Every e2e entry used to fall back to one absolute path on the machine the
 * suite was written on, so on any other machine the fallback failed with a
 * message about somebody else's disk. Candidates are tried in order and a
 * failure names PLAYWRIGHT_MODULE, which is the one thing a runner can set.
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
// playwright.mjs lives at <root>/src/campus/tests/, so the repository root is
// three levels up. Deriving it from this file rather than process.cwd() means a
// report is the same whether the entry ran from the root or from anywhere else.
const repoRoot = path.resolve(moduleDir, '..', '..', '..');

/**
 * Record an artifact path relative to the repository root.
 *
 * A report that stores the absolute path it happened to run from leaks the
 * machine layout into a delivered artifact, and two runs from different
 * checkouts then differ for no reason. Forward slashes keep the value identical
 * on Windows and POSIX.
 */
export function relativeArtifact(absolute) {
  return path.relative(repoRoot, path.resolve(absolute)).split(path.sep).join('/');
}

export function loadPlaywright() {
  const candidates = [];
  if (process.env.PLAYWRIGHT_MODULE) candidates.push(process.env.PLAYWRIGHT_MODULE);
  candidates.push('playwright');
  candidates.push(path.join(process.cwd(), 'node_modules', 'playwright'));
  const failures = [];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      failures.push(`${candidate} (${error.code || error.message})`);
    }
  }
  throw new Error(
    'Playwright is not installed.\n'
    + 'Install it, or point PLAYWRIGHT_MODULE at an installed Playwright module.\n'
    + failures.map((line) => `  tried ${line}`).join('\n'));
}
