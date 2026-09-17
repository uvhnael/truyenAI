# Frontend verification

- Next.js 16.3.5 / React 19.3.0, TypeScript 5.9; package-lock committed to workspace (no Git commit made).
- `npm run build`: exit 0; optimized build, TypeScript, static pages (3/3) all successful. Routes: `/` static, `/projects/[id]` dynamic.
- `npm run typecheck`: exit 0.
- `npm audit --omit=dev`: 0 vulnerabilities.
- `npm run start -- --port 13000`: tested actual standalone output via scripts/start.mjs; copies static/public assets before launch. Docker runs standalone server directly.
- Browser exercised actual parent API at localhost:18080 (mock LLM, real PostgreSQL/Redis worker): project create with null options, navigation, SSE activity replay, completed chapters, bible including structured character arcs, outline, chapter title save, revisions, and stale-version 409 conflict preserving unsaved content.
- Editing verified available for `needs_review` with job `done`. Running/queued/failed/cancelled states are locked by frontend policy; stale saves also checked by backend.
- Mobile viewport 390px: document scrollWidth 390px, no horizontal page overflow. Desktop and mobile visually inspected.
- Final production server routes returned HTTP 200; root language vi; explicit demo banner present.

## Runtime notes

`NEXT_PUBLIC_API_URL` is baked at build time; default http://localhost:18080. Docker ARG matches. Web host expected localhost:13000, container port 3000.

Optional chapter/word counts default to blank/null. Render resolved values from `spec` when API `options` omits them. Mock backend may choose fixed demo counts rather than semantically parsing the prompt.

React must render character `arc` through ObjectView, not directly: the API can return `{start, end}` rather than text. Backend chapter status `final` and project status `needs_review` are translated.

Browser test project: c1390c37-bdbe-465e-aad6-d7c221083ed1, title “Kiểm thử giao diện — Đà Lạt”; edits generated revision 3 and an intentional concurrent server edit generated revision 4. These are real test records in the local backend.

No live Gemini call or Docker image build performed by the frontend subagent. No synthetic API used for reported browser checks.
