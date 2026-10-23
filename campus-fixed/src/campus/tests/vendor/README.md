# Vendored test-only dependencies

Nothing here ships in the built application. These files exist so the acceptance
scripts can run without reaching the network at test time.

## axe.min.js

- Package: [axe-core](https://github.com/dequelabs/axe-core) 4.10.2
- Source: `https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js`
- SHA-256: `b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3`
- License: Mozilla Public License 2.0 (see `axe-core-LICENSE.txt`)

`src/campus/tests/a11y.e2e.mjs` injects this file into the page under test and
runs it there. The runner refuses to proceed if the file is missing, so an
accessibility run can never silently degrade into "no checks performed".
