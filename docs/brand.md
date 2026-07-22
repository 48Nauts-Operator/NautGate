# NautGate brand

Locked 2026-07-22. Chosen by testing twelve candidate hexes against the
dashboard's own status palette — see the `NautGate` Paper file
(*Colour directions*, *Hex candidates*, *Sage vs Asparagus*).

## Register: terminal, not SaaS

Pure black ground, hairline rules instead of cards, mono-first labelling, one
solid accent block for the primary action. NautGate is a measuring instrument
and the page should look like one. The hero leads with the *accusation* — "ask
a model who it is and it lies" — not a feature list.

Chosen over a matrix-green variant of the same layout (also on canvas). Green
had more voltage but is the default hacker signal, collides with `--ng-good`,
and reads wrong in a compliance conversation. Olive is rarer, more conservative,
and reads *instrument* rather than *hacker*.

## Palette

```css
:root {
  /* brand — olive */
  --ng-brand:      #808000;  /* CTA block, logo mark, rules */
  --ng-brand-mid:  #A3AB00;  /* borders, hover */
  --ng-brand-lift: #C3CE1F;  /* text, links, stat figures, machine values */
  --ng-brand-pale: #DDE86B;  /* rare headline accent */

  /* ground + type */
  --ng-ground:     #000000;  /* page */
  --ng-surface:    #050502;  /* inset panels, record blocks */
  --ng-line:       #2A2E14;  /* hairlines — olive-tinted, never neutral grey */
  --ng-paper:      #F0F0E4;  /* headlines */
  --ng-body:       #A8AD86;  /* body copy */
  --ng-faint:      #7E8748;  /* mono labels, captions */

  /* semantic — NEVER used for brand, NEVER overridden */
  --ng-good:       #3FB950;
  --ng-warn:       #D6A100;
  --ng-bad:        #E5484D;
  --ng-info:       #4C8DFF;
}
```

Hairlines are olive-tinted (`#2A2E14`), not neutral grey — that's what makes the
grid feel like one material rather than a dark theme with an accent bolted on.

## The one rule

**The accent never encodes data.**

Olive is chrome: logo, buttons, links, section labels, stat figures on marketing
pages. Inside tables, charts and status indicators only the semantic palette
speaks — green healthy, amber warn, red failing, blue info.

This is why olive works and why red didn't. A brand hue that also carries
meaning destroys the meaning: if the logo is red, a real failure stops being
loud. Olive sits nearest `--ng-warn`, so amber is the one to keep clear of.

Which leaves charts needing a third palette of their own — neither brand nor
semantic:

```css
--series-1: #7C9BFF;  /* periwinkle */
--series-2: #B98CF0;  /* violet */
--series-3: #4FC7C3;  /* teal */
--series-4: #E38FB4;  /* rose */
```

Cool and mid-saturation, so a red bar or a green dot inside the same chart
still reads as *failing* / *healthy* rather than as just another category. The
first cut of this reused `--ng-good` and `--ng-info` as series 2 and 3, which
quietly broke the rule from the other direction: the semantic hues have to stay
as scarce as the brand one.

## Why a ramp, not one hex

`#808000` is too dark to read as text on near-black — measured, not assumed.
Fills stay at the base; anything typographic lifts to `--ng-brand-lift`. A
single flat tone makes everything it touches go quiet (verified against
`#8A9A5B`, which lost all hierarchy until it got the same ramp).

## Type

The 48Nauts system, already self-hosted on xnaut.dev and nautloop — use the same
three files rather than introducing a fourth face:

| Face | Role | File |
|---|---|---|
| **Space Grotesk** | display — headlines, the wordmark, stat figures | `space-grotesk.woff2` |
| **Inter** | body copy, UI, everything at reading size | `inter.woff2` |
| **JetBrains Mono** | labels in caps, and every machine value — model ids, costs, status, commands | `jetbrains-mono.woff2` |

Space Grotesk carries the terminal register better than a neutral grotesque: it
is geometric and slightly mechanical without being a novelty face. Inter does the
reading. Mono is reserved — see the rule at the end of this file.

Copy the woff2 files from `xNAUT/website/fonts/`. Latin subset only.

## Scale

| Role | Size / line-height | Weight |
|---|---|---|
| Hero | 52 / 52, `-0.035em` | 700 (Space Grotesk max) |
| Section | 30–38 / 44, `-0.02em` | 700 |
| Stat figure | 26 | 700 |
| Body | 16 / 25 | 400 |
| Small body | 14 / 21 | 400 |
| Mono label | 10–11, `0.16em`, caps | 500 |

## Portfolio separation

xNAUT is yellow, NautLoop is blue, NautGate is olive. Olive is dark and
desaturated where xNAUT's yellow is bright and saturated — they don't collide in
a lineup.

## Rejected, and why

| Hex | | Reason |
|---|---|---|
| `#FF0800`, `#A81C07`, `#B4122E` | reds | Indistinguishable from `--ng-bad`. A brand that reads as failure. |
| `#C46210` | burnt ochre | Sits on `--ng-warn`, and still orange — the thing we left. |
| `#8B5CF6` | violet | Dimmest of all candidates as small text on near-black; also the spent 2019–2024 dark-SaaS accent. |
| `#FF2D78` | hot pink | Strong and legible, but hard to place in a compliance conversation. |
| `#2DD4BF`, `#C8FF1E` | mint, lime | Green-family — crowd `--ng-good`. |
| `#7BA05B` | asparagus | Nearly identical hue to `--ng-good`. |
| `#6C7C59` | moss | Too dim; washes out on black. |
| `#4090BD`, `#38E1FF` | blues | Cyan was the strongest non-olive candidate; blue is NautLoop's. |
| `#00E83C` | matrix green | Strong, but the default hacker signal; collides with `--ng-good`; wrong register for compliance. Kept on canvas as the louder variant. |

## The mark

Rounded square, brand colour, one knocked-out form — the same system as its
siblings. **xNAUT** is `xN` on yellow, **NautLoop** is `∞` on blue, **NautGate**
is a visor and a three-block mouth on olive.

```
rect  0    0    32   32   rx 7     #808000   the gate
rect  4.5  8.5  23   6.6  rx 1     #0A0B00   the eye — it watches
rect  7.5  19.8 5    5    rx 0.9   #0A0B00   ┐
rect  13.5 19.8 5    5    rx 0.9   #0A0B00   ├ the mouth — it reports
rect  19.5 19.8 5    5    rx 0.9   #0A0B00   ┘
```

A gateway that watches everything through and says what it saw. `-naut` is the
voyager (Greek ναύτης, sailor — same root as Sanskrit नौ, boat); NautGate is the
threshold they pass through.

Files in `assets/brand/`: `icon.svg`, `icon-inverse.svg` (for olive grounds),
`logo.svg`, `logo-mono.svg` (uses `currentColor`), plus rendered `og.png`
(1200×630), `x-header.png` (1500×500), `avatar.png`, `icon-512.png`,
`apple-touch-icon.png`. Regenerate the rasters from `.build/*.html`.

### Rejected marks

Shields (generic security), seals and clipped corners (bureaucratic rather than
evidential), apertures and stargates (spidery at 16px). And **not `>`** — a
chevron in a rounded square is the universal play button.

## Type in the terminal register

Body copy below the fold is **Inter**, not mono. A full page of monospace
is tiring — mono is reserved for labels, machine values (model ids, costs,
status) and the command line. This is the thing most likely to break the
direction at length, so hold it.
