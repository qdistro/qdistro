# Scenario report — 06-qt-admin-app-mouse

Verdict: **FAIL**

- S1 PASS: baseline showed `uid=2000 test.action`; the reviewed click-preview for `1 hour` was correctly placed, and the post-click frame showed `1 hour` selected while the pending row remained. Evidence: `s1a-r2.png`, `click-targets/click-003.annotated.png`, `click-targets/click-003.zoom.png`, `s1b-r2.png`.
- S2 PASS: the reviewed Approve preview was correctly placed; the post-click frame showed an empty list and `(no selection)`. The SDK oracle was `ALLOWED`, `rc=0`. Evidence: `click-targets/click-004.annotated.png`, `click-targets/click-004.zoom.png`, `s2-r2.png`, `oracles-r2.log`.
- S3 partial: the second SDK call returned `ALLOWED`, `rc=0`, but the required post-cache-hit frame was fully black and the admin-app process was absent, so the required empty-list/no-error visual assertion could not pass. Evidence: `s3-r2.png`, `oracles-r2.log`.

The initial S3 attempt also retained rejected near-black captures; they were not deleted or overwritten.
