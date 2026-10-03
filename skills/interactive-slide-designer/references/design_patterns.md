# Design Patterns & Theme Palettes for Interactive HTML5 Proposals

## 1. Supported Theme Palettes (`theme_color`)

Each presentation supports dynamic accent theming across headers, badges, borders, progress bars, and callout cards:

| `theme_color` | Primary Accent | Secondary Accent | Recommended Industry / Tone |
| :--- | :--- | :--- | :--- |
| `sky` (default) | `sky-400` / `sky-500` | `indigo-500` | Enterprise DX, Cloud Architecture, Retail OMO |
| `emerald` | `emerald-400` / `emerald-500` | `teal-500` | Sustainability, Financial Services, ROI-driven Ops |
| `violet` | `violet-400` / `violet-500` | `fuchsia-500` | Creative UX/CX, AI Agents, Digital Media |
| `amber` | `amber-400` / `amber-500` | `orange-500` | Commerce, Consumer Brand, Growth Marketing |
| `rose` | `rose-400` / `rose-500` | `pink-500` | Healthcare, Hospitality, Customer Loyalty |

## 2. Typography & Viewport Rules

- **Headings**: `Plus Jakarta Sans` + `Noto Sans JP`, weights `700`/`800`/`900`, tight tracking.
- **Technical Labels & Counters**: `JetBrains Mono` uppercase with `tracking-widest`.
- **Slide Density Guardrails**:
  - Max 3 cards per row on Challenges / CX Highlights / Roadmap phases.
  - Max 4 architecture layers in left-to-right flow on Slide 04.
  - Use concise Japanese executive copy (titles <= 28 chars, body descriptions <= 85 chars) so content never overflows a `100vh` 16:9 viewport.
