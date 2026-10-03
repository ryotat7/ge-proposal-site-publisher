---
name: interactive-slide-designer
description: >-
  Generates and validates bespoke, executive-grade 16:9 interactive HTML5
  presentation slide decks with per-slide visual layouts (Hero cover, Bento KPI
  grid, As-Is/To-Be comparison matrix, 4-layer architecture flow, 3-phase
  roadmap timeline, and ROI action matrix), Tailwind CSS, GSAP animations, and
  keyboard/button navigation. Use when synthesizing or editing client proposal
  presentation websites via Managed Agents API or ADK tools.
---

# Interactive Slide Designer Skill

Produces executive-grade, single-file interactive HTML5 slide decks (`index.html`) tailored to each client's industry, visual tone, and strategic narrative.

## 1. Non-Negotiable Structural Contract

Every generated or edited HTML presentation (`index.html`) MUST satisfy all of the following DOM invariants (enforced by `scripts/validate_slide_deck.py`):

1. **Document Root**: Starts with `<!DOCTYPE html>` and `<html lang="ja">` (or `<html lang="en">`).
2. **Zero Unexpanded Placeholders**: Must NOT contain literal `{{` or `}}` tokens anywhere in the final HTML.
3. **16:9 Full-Viewport Slide Container**:
   - `body` has `width: 100vw; height: 100vh; overflow: hidden;`.
   - Exactly 6 `<section class="slide ..."` elements with sequential `data-slide-index="0"` through `data-slide-index="5"` and a distinct `data-layout` attribute on each slide.
4. **Required CDN Assets**:
   - Tailwind CSS (`https://cdn.tailwindcss.com`)
   - GSAP 3.12.2 (`gsap.min.js`)
   - FontAwesome 6.4.0 (`font-awesome`)
   - Google Fonts (`Plus+Jakarta+Sans`, `Noto+Sans+JP`, `JetBrains+Mono`)
5. **Interactive Navigation & Progress Bar**:
   - `#slide-counter` showing `01 / 06` through `06 / 06`
   - `#progress-bar` updating width from `16.67%` to `100%`
   - `#prev-btn` and `#next-btn` click handlers
   - `keydown` listener supporting `ArrowRight`, `ArrowLeft`, and `Space`

## 2. Bespoke Per-Slide Layout Architecture

Never repeat the same 3-column card grid on every slide. Assign a distinct layout archetype (`data-layout`) to each slide:

| Slide | `data-slide-index` | `data-layout` | Visual Structure |
| :--- | :--- | :--- | :--- |
| **Slide 01** | `0` | `hero-cover` | Asymmetric Executive Cover: ambient gradient glow, industry badge, client addressee header, high-impact title, left-bordered value proposition callout, and metadata bar. |
| **Slide 02** | `1` | `bento-executive-summary` | Asymmetric Bento Grid: 3 diagnostic challenge cards (with impact tags) + full-width strategic conclusion banner with highlighted KPIs. |
| **Slide 03** | `2` | `as-is-to-be-comparison` | Split As-Is (Before) vs. To-Be (After) comparison matrix + 3 bottom UX/AI transformation highlight cards with icons. |
| **Slide 04** | `3` | `architecture-flow` | 4-Layer Left-to-Right System & Data Integration Architecture with SVG directional connectors, component chips, and zero-trust governance footer. |
| **Slide 05** | `4` | `roadmap-timeline` | 3-Phase Horizontal Roadmap Timeline (`Phase 1` -> `Phase 2` -> `Phase 3`) with period badges, deliverable checklists, and milestone flags. |
| **Slide 06** | `5` | `roi-and-next-steps` | Two-column Impact & Action Matrix: quantitative ROI metric banners + qualitative benefits on the left, numbered immediate action plan on the right. |

See [references/design_patterns.md](references/design_patterns.md) for theme color palettes (`sky`, `emerald`, `violet`, `amber`, `rose`) and HTML/CSS snippets.

## 3. Managed Agents API & Editing Workflow

- **New Deck Creation**:
  1. Ground the storyline using internal knowledge search results and user consultation notes.
  2. Select the accent theme (`sky`, `emerald`, `violet`, `amber`, `rose`) matching the client's brand or request.
  3. Generate the 6-slide HTML5 document adhering to the 6 `data-layout` archetypes.
  4. Validate with `python3 scripts/validate_slide_deck.py <path_to_index.html>`.
- **Live Deck Editing**:
  1. Load the existing `index.html` and structured `deck_spec` metadata.
  2. Apply the user's natural-language modifications (title, copy, KPIs, roadmap items, or `theme_color`) while preserving all 6 slides (`data-slide-index="0"`..`"5"`) and navigation scripts.
  3. Re-run `validate_slide_deck.py` before uploading back to Cloud Storage.
