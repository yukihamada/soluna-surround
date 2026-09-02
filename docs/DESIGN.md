# SOLUNA Sound — design system

One mood everywhere: **night sky, gold sun / pale moon**. Cream text on ink. Numbers in mono.
Every screen has **one obvious action** and answers, in the first second, "what is this, what do I do".

Shared CSS: `/ui/soluna.css` (tokens + components). Pages link it and add only page-specific rules.

| Token | Value | Use |
|---|---|---|
| `--ink` `#0a0507` | page background | never pure black |
| `--panel` `#161014` | cards | with 1px `--line` border, radius 20 |
| `--txt` `#f2e7d3` / `--dim` `#9d8d76` | text / secondary | never grey-on-grey below 4.5:1 |
| `--gold` `#d4af37` | the one accent: primary action, brand, active | one gold thing per viewport |
| `--moon` `#c8d3e6` | secondary accent (video/screen/moon) | sparingly |
| `--ok` `--warn` `--bad` | status only | LEDs, tiles, banners |
| `--sans` Zen Kaku Gothic New | UI text | ja+en in the same face |
| `--serif` Shippori Mincho | titles on audience-facing screens | brand voice |
| `--mono` IBM Plex Mono | numbers, IDs, brand wordmark | tabular nums |

Components (class → intent): `.s-bar` sticky header · `.s-card` + `h2 small` section with plain-language subtitle ·
`.s-btn.primary|ghost|danger|big` · `.s-input` · `.s-led.on|bad|warn` · `.s-pill` · `.s-tile` KPI ·
`.s-kv` key/value · `.s-hint` (+`.note`) · `.s-toast` · `.s-table` · `details.s-fold` progressive disclosure.

Rules of clarity
1. **Headline in words, not jargon**: "何台が本当に鳴っているか" beats "DEVICES". Keep the English word as the small label.
2. **Step numbers where there is an order** (`h2 .step`): 1 upload → 2 preload → 3 fire.
3. **Progressive disclosure**: audience sees ▶ and the zone; details fold. FOH sees show controls first; setup last.
4. **Status is a sentence**: "接続 128 · 鳴っている 121 · 準備OK 5 · 失敗 2" with LEDs, not a JSON dump.
5. **Destructive = red and confirmed**. Primary = gold, exactly one per card.
6. **ja first, en small** on audience/crew pages; FOH console is ja with en labels; setup shows both.
7. Mobile first (375 px), fine on 1280. Touch targets ≥ 44 px. Respect `prefers-reduced-motion`.
8. Never change element IDs or JS behaviour when restyling — tests and E2E depend on them.
