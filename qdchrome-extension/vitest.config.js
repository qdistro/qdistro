import { defineConfig } from 'vitest/config';

// Vitest 4 removed coverage.all: executed files are always reported, but a
// src file no test ever loads would silently leave the denominator instead of
// counting as uncovered. Pinning include to src/ keeps that property — and
// keeps executed test helpers (tests/) out of the qci floor measurement in
// ci/coverage-floors.tsv.
export default defineConfig({
    test: {
        coverage: {
            include: ['src/**/*.js'],
        },
    },
});
