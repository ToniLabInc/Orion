// Horizon's shaders (GLSL ES 3.0). Everything is measured in CSS pixels from the viewport's top left, so a line is as
// thick, a star as big and a glow as wide on every screen density; the device's pixels only make it sharper. The limb
// is a circle (uLimb); a point's altitude above it is the exact distance to that circle (never a difference of two
// large numbers). No bloom, no blur: a glow is an analytic falloff round a thin line, and a thin line is a Gaussian
// integrated over the pixel that holds it.
//
//   BAND        the world's regions as geometry: an annular sector of the limb, as a strip of radial quads whose vertices
//               carry their altitude and arc length (the rasteriser interpolates both exactly along a radius), so no
//               pixel works out an altitude or an atan. The deep world is a clear; the far sky is one strip, the band
//               round the limb another, the aurora's texture a third.
//   SKY         the far sky: its colour, a trace of the glow's haze, the baked Milky Way, M42.
//   NEAR        the band round the limb (eighty px below it to two hundred above, taller while the aurora shows): the
//               airglow string and its glow, the night side's lit cloud tops, the aurora's texture added on, a triangular
//               dither (the sky is so dark that without it its gradients would band).
//   AURORA      the curtains, into a texture of their own while they show.
//   STAR        the catalogue, one instanced quad per star: its light as a photograph shows it (a Gaussian integrated
//               over each pixel, a Moffat wing for the brightest, an exposure that saturates), reddened and dimmed near
//               the limb, twinkling more the lower it stands.
//   STROKE      single strokes of light, as quads round each segment: the pen's writing and the Orion figure drawing itself.
//   METEOR      a ribbon from the limb to a point, a head and a tail.
//
// Everything after the background blends onto it ("screen" for stars and meteors, "max" for strokes, so that two
// segments meeting at a joint don't add up to a bead). All of it writes sRGB-encoded colour: the dark sky has to be
// dithered after the encode, so the blending is of display values, which for light over near-black is indistinguishable.
// Textures are read with textureLod: nothing here needs a derivative, and the branches may then diverge freely.
//
// The glsl tags below mark the template literals the build minifies (vite.config.ts): no block comments inside them,
// and `${}` only on a line of its own. (GLSL ES reserves `half`, `patch`, `sample`, `input`, `filter`... as names.)

const HEAD = /* glsl */ `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
const float PI = 3.14159265;
const float TAU = 6.28318531;
`

/** Colour, random numbers and the Gaussian's integral. */
const TOOLS = /* glsl */ `
// linear light to display values; nearly every pixel of the sky and the ground is in the linear segment: no pow there
vec3 encode(vec3 c) {
  c = clamp(c, 0.0, 1.0);
  if (max(c.r, max(c.g, c.b)) < 0.0031308) return c * 12.92;
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}
// the PCG hash, Jarzynski and Olano, Hash Functions for GPU Rendering (JCGT 2020)
uint pcg(uint v) {
  uint s = v * 747796405u + 2891336453u;
  uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
  return (w >> 22u) ^ w;
}
float rnd(uint v) { return float(pcg(v) >> 8u) * (1.0 / 16777216.0); }
// a triangular dither of one display level, from Jimenez's interleaved gradient noise (four operations a sample)
float dither(vec2 p) {
  float a = fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
  float b = fract(52.9829189 * fract(dot(p + vec2(5.3, 11.7), vec2(0.06711056, 0.00583715))));
  return (a + b - 1.0) * (1.0 / 255.0);
}
// the Gaussian's cumulative distribution (a tanh fit, within 1e-3); tanh's argument is clamped, since where it is huge
// some drivers answer NaN, and a NaN is a black pixel
float cdf(float x) { return 0.5 + 0.5 * tanh(clamp(0.7978845608 * (x + 0.044715 * x * x * x), -12.0, 12.0)); }
// a line's profile averaged over the pixel's footprint w along it: a Gaussian of deviation s, a from its middle, unit area
float profile(float a, float s, float w) { return (cdf((a + 0.5 * w) / s) - cdf((a - 0.5 * w) / s)) / w; }
`

/** The viewport and the limb. */
const GEOMETRY = /* glsl */ `
uniform vec2 uCss;   // the viewport, CSS px
uniform vec2 uPx;    // device px per CSS px (across, down)
uniform vec4 uLimb;  // centre x, centre y, radius, the limb's top (CSS px)
// the height above the limb, CSS px (negative inside the world): (d² − r²) / (d + r), the numerator taken exactly
float altitude(vec2 p) {
  float dx = p.x - uLimb.x;
  float e = p.y - uLimb.w;
  float d = length(vec2(dx, uLimb.z - e));
  return (dx * dx + e * (e - 2.0 * uLimb.z)) / (d + uLimb.z);
}
// the arc length along the limb from its top to the point under p
float arc(vec2 p) { return uLimb.z * atan(p.x - uLimb.x, uLimb.y - p.y); }
vec4 clipPos(vec2 p) { return vec4(p.x / uCss.x * 2.0 - 1.0, 1.0 - p.y / uCss.y * 2.0, 0.0, 1.0); }
`

/** Where a fragment is, in CSS px (fragment shaders only: a vertex shader has no gl_FragCoord). */
const FRAG = /* glsl */ `
vec2 cssPos() { return vec2(gl_FragCoord.x / uPx.x, uCss.y - gl_FragCoord.y / uPx.y); }
`

/** The string's displacement, slope and smear at an arc length. */
const STRING = /* glsl */ `
uniform sampler2D uString;  // one row of nodes: displacement (CSS px), slope along the limb, smear (CSS px)
uniform vec2 uSpan;         // where the string starts along the limb (CSS px from its top), and 1 / its length
vec3 stringAt(float s) {
  int n = textureSize(uString, 0).x;
  float x = clamp((s - uSpan.x) * uSpan.y, 0.0, 1.0) * float(n - 1);
  int i = int(floor(x));
  float f = x - float(i);
  vec3 a = texelFetch(uString, ivec2(i, 0), 0).xyz;
  vec3 b = texelFetch(uString, ivec2(min(i + 1, n - 1), 0), 0).xyz;
  return mix(a, b, f);
}
`

/** The sky's place on the screen: a direction to a point (stereographic, as sky.ts). */
const SKY = /* glsl */ `
uniform mat3 uViewInv;  // the view's rows are its columns: dir * uViewInv is the view's x, y, z
uniform vec4 uFrame;    // the sky: CSS px per radian, the centre's x and y, how open it is (0..1)
uniform vec2 uShift;    // the drift and the parallax, CSS px
vec2 place(vec3 q) {
  float k = 2.0 / (1.0 + q.z);
  return vec2(uFrame.y + q.x * k * uFrame.x, uFrame.z - q.y * k * uFrame.x) + uShift;
}
`

/** The sky's light at a point: the same for the far sky and the near band. */
const SKYLIGHT = /* glsl */ `
uniform sampler2D uNoise;
uniform sampler2D uMilkyWay;  // the band, baked by the CPU (sky.ts, milkyWay)
uniform vec4 uMW;        // where it is: the margin round the viewport (CSS px), 1 / its width and 1 / its height, its peak light
uniform vec4 uGlow;      // the glow's light, his voice's level, yours, the loading sweep's place along the limb
uniform vec3 uColB;      // the airglow's colour now, and the one it turns to higher up
uniform vec3 uUpB;
uniform vec3 uSky;
uniform vec3 uNebula;
uniform vec3 uDust;
uniform vec4 uM42;       // M42 in the sky's plane: x, y, deviations across and up (radians)
${SKY}
vec4 noise(vec2 uv) { return textureLod(uNoise, uv, 0.0); }

// the sky at a point and height above the limb: its colour, the glow's haze and tail, the Milky Way, M42
vec3 skyAt(vec2 p, float a) {
  float h = 1.0 - clamp(a / max(uLimb.w, 1.0), 0.0, 1.0);
  vec3 lin = uSky + mix(uColB, uUpB, 0.6) * (uGlow.x * 0.0011 * h * h * sqrt(h));
  // the glow's long tail belongs to the sky, so the near band and the far sky agree where they meet
  if (a < 300.0) lin += uUpB * (uGlow.x * 0.0085 * exp(-a / 62.0));
  if (uFrame.w > 0.0) {
    vec2 ps = p - uShift;
    lin += uDust * (uMW.w * uFrame.w * textureLod(uMilkyWay, (ps + uMW.x) * uMW.yz, 0.0).r);
    vec2 pl = vec2(ps.x - uFrame.y, uFrame.z - ps.y) / uFrame.x;
    vec2 dn = (pl - uM42.xy) / uM42.zw;
    float d2 = dot(dn, dn);
    // M42, the Orion Nebula: a faint pink smudge with wisps
    if (d2 < 16.0) lin += uNebula * (0.06 * exp(-0.5 * d2) * (0.45 + noise(pl * 7.0 + 0.3).a) * uFrame.w);
  }
  return lin;
}
`

// ----------------------------------------------------------------------------------------------------- the band
/** A strip of radial quads over a range of altitudes, running from one side of the limb to the other. */
export const BAND_VERTEX = /* glsl */ `${HEAD}${GEOMETRY}
uniform vec4 uBand;   // its lowest and highest altitude (CSS px), how far round the limb it runs each way (radians), its cells
out float vA;         // altitude above the limb
out float vS;         // arc length along it from its top
out vec2 vN;          // the limb's outward normal there

void main() {
  int cell = gl_VertexID >> 1;
  float a = (gl_VertexID & 1) == 0 ? uBand.x : uBand.y;
  float theta = mix(-uBand.z, uBand.z, float(cell) / uBand.w);
  vN = vec2(sin(theta), -cos(theta));
  vA = a;
  vS = uLimb.z * theta;
  gl_Position = clipPos(uLimb.xy + vN * (uLimb.z + a));
}
`

export const SKY_FRAGMENT = /* glsl */ `${HEAD}${TOOLS}${GEOMETRY}${FRAG}${SKYLIGHT}
in float vA;
out vec4 outColor;

void main() {
  outColor = vec4(encode(skyAt(cssPos(), vA)) + dither(gl_FragCoord.xy), 1.0);
}
`

// One band round the limb. What is off costs nothing: uLive says what is on (a string that moves, flares, your voice,
// the loading sweep), and a branch on a uniform is free.
export const NEAR_FRAGMENT = /* glsl */ `${HEAD}${TOOLS}${GEOMETRY}${FRAG}${STRING}${SKYLIGHT}
uniform sampler2D uAurora;
uniform vec2 uAurSeen;   // whether the aurora's texture is added (0 or 1), the curtains' top altitude (CSS px)
uniform float uPatch;    // a slow phase, 0..1: the glow's drifting patches (the noise is one tile: it wraps unseen)
uniform vec3 uColA;      // the airglow's colour, from his last state to his state now (uColB): it spreads from the talk button
uniform vec3 uUpA;
uniform vec3 uWipe;      // the talk button's place along the limb, how far the new colour has spread, its softness
uniform vec4 uBump[4];   // flares: place along the limb, width, strength
uniform vec4 uLive;      // what is on: the string moves (0 or 1), how many flares, your voice (0 or 1), the loading sweep (0 or 1)
uniform vec3 uGround;
in float vA;
in float vS;
in vec2 vN;
out vec4 outColor;

void main() {
  vec2 p = cssPos();
  float a = vA;
  float s = vS;
  vec3 str = uLive.x > 0.5 ? stringAt(s) : vec3(0.0);
  float ad = a - str.x;

  // the airglow's colours spread from the talk button
  vec3 core = uColB;
  vec3 up = uUpB;
  if (uWipe.y < 1.0e5) {
    float m = 1.0 - smoothstep(uWipe.y - uWipe.z, uWipe.y, abs(s - uWipe.x));
    core = mix(uColA, uColB, m);
    up = mix(uUpA, uUpB, m);
  }

  // the light along the limb: slow patches, his voice, yours at the talk button, flares, the loading sweep
  float along = 1.0 + 0.34 * (noise(vec2(s / 1700.0 + uPatch, 0.31)).a - 0.5) + 0.05 * sin(TAU * uPatch * 4.0);
  along *= 0.82 + 0.5 * uGlow.y;
  if (uLive.z > 0.5) {
    float dm = (s - uWipe.x) / (0.22 * uCss.x);
    along *= 1.0 + 0.9 * uGlow.z * exp(-dm * dm);
  }
  for (int i = 0; i < int(uLive.y); i++) {
    float db = (s - uBump[i].x) / uBump[i].y;
    along += uBump[i].z * exp(-0.5 * db * db);
  }
  if (uLive.w > 0.5) {
    float dw = (s - uGlow.w) / (0.06 * uCss.x);
    along += 1.4 * exp(-0.5 * dw * dw);
  }
  float light = uGlow.x * along;

  vec3 lin;
  if (a < 0.0) {
    // the night side: cloud decks under starlight, lit at their edge by the glow, a little squeezed towards the limb
    lin = uGround;
    float depth = max(-ad, 0.0);
    if (depth < 70.0) {
      float z = 1.0 / (depth / 70.0 + 0.5);
      float cl = noise(vec2(s / 1500.0 * z * 0.22 + uPatch, z * 0.07)).a;
      lin += light * 0.045 * exp(-depth / 15.0) * (0.35 + 1.15 * cl) * mix(core, up, 0.35);
    }
  } else {
    lin = skyAt(p, a);
  }

  // the airglow: the string, a thin sharp core with a halo that falls both ways, over the limb's own atmosphere, a
  // gradient above the world's edge that turns from his colour to its upper one (it stays with the limb when the
  // string lifts: a bright wire over a still haze)
  if (a > -40.0 && a < 250.0) {
    float sig = 0.55;
    float sg = sqrt(sig * sig + str.z * str.z / 3.0);          // the exposure smears a moving string
    float d = ad * inversesqrt(1.0 + str.y * str.y);
    float above = max(a, 0.0);
    float w = dot(abs(vN), 1.0 / uPx);
    float atmosphere = above < 120.0 ? 0.14 * exp(-above / 3.2) + 0.06 * exp(-above / 15.0) : 0.0;
    vec3 glow = atmosphere * clamp(a / w + 0.5, 0.0, 1.0) * mix(core, up, smoothstep(0.0, 70.0, above));
    if (abs(d) < 4.0 * sg + w) glow += profile(d, sg, w) * 2.5066 * sig * mix(core, vec3(1.0), 0.42);
    if (abs(d) < 40.0) glow += 0.2 * exp(-abs(d) / 3.2) * core;
    lin += light * glow;
  }

  // the aurora, drawn at its own resolution while it shows
  if (uAurSeen.x > 0.5 && ad > -2.0 && ad < uAurSeen.y + 8.0)
    lin += textureLod(uAurora, gl_FragCoord.xy / (uCss * uPx), 0.0).rgb;

  outColor = vec4(encode(lin) + dither(gl_FragCoord.xy), 1.0);
}
`

// ----------------------------------------------------------------------------------------------------- aurora
export const AURORA = /* glsl */ `${HEAD}${TOOLS}${GEOMETRY}${STRING}
uniform sampler2D uNoise;
uniform sampler2D uRays;  // one line of baked noise, four scales (noise.ts, makeRays): a texel is 0.8 CSS px, it tiles every 3277 px
uniform vec4 uAur;       // his thinking 0..1, the curtains' top altitude (CSS px), how many sheets (1 or 2), the drift (CSS px)
uniform vec4 uBump[4];
uniform vec4 uLive;      // how many flares (in .y)
uniform vec3 uAuLow;     // a curtain's bright lower border
uniform vec3 uAuHigh;    // and its tops
uniform float uTime;
in float vA;
in float vS;
out vec4 outColor;

vec4 noise(vec2 uv) { return textureLod(uNoise, uv, 0.0); }

// one curtain sheet: where it stands along the limb, how it folds, its fine rays and their heights
vec3 sheet(float s, float ad, float k, float amount, float fl) {
  float W = uCss.x;
  float dr = uAur.w;
  float cover = smoothstep(0.40, 0.62, noise(vec2((s + dr * 0.35) / (1.35 * W) + 0.173 * k, 0.5 + 0.31 * k)).g);
  if (cover <= 0.0) return vec3(0.0);
  // folds: the sheet leans and bends, and where it is steep it is seen edge-on and shines brighter
  vec2 fuv = vec2((s + dr * 0.55) / (0.5 * W) + 0.41 * k, dr * 0.0006 + 0.7 * k);
  float f0 = noise(fuv).r;
  float f1 = noise(fuv + vec2(0.004, 0.0)).r;
  float fold = (f0 - 0.5) * 0.12 * W;
  float slope = (f1 - f0) * 0.12 * W / (0.004 * 0.5 * W);
  float edge = 0.45 + 0.85 * clamp(abs(slope), 0.0, 2.2);
  // rays, from one fetch: r the rays (3.2 px), g finer ones, b the height each stands to, a its lower border
  float q = s + fold + dr * 0.8 + ad * (f0 - 0.5) * 0.3;   // rays lean a little with the fold, all the same way
  vec4 r = textureLod(uRays, vec2(q / 3276.8 + 0.37 * k, 0.5), 0.0);
  float rays = clamp(r.r * 0.75 + r.g * 0.5 - 0.2, 0.0, 1.0);
  if (rays <= 0.0) return vec3(0.0);
  rays *= rays * (3.0 - 2.0 * rays);
  // each ray's height (most are short, a few stand tall), and a lower border that is sharp but not straight
  float H = uAur.y * (0.22 + 0.78 * r.b * r.b) * (0.55 + 0.45 * amount) * (1.0 + 0.7 * fl) / 1.7;   // never past the top
  if (ad > H) return vec3(0.0);
  float low = 1.5 + 3.5 * r.a;
  float prof = smoothstep(low - 1.2, low + 2.5, ad) * exp(-max(ad - low, 0.0) / (0.4 * H))
    * (1.0 - smoothstep(0.6 * H, H, ad));
  // pulses climbing the rays, each patch of them at its own pace and phase
  float shim = 0.7 + 0.3 * sin(TAU * uTime * (1.1 + 0.6 * r.b) - ad * 0.05 + 6.0 * r.b);   // a pulse climbs a whole patch of rays
  vec3 col = mix(uAuLow, uAuHigh, smoothstep(0.0, 0.8 * H, ad)) * (1.0 + 1.5 * exp(-ad / 7.0));
  return col * (cover * rays * prof * shim * edge * amount * (1.0 + 1.1 * fl) * 0.52);
}

void main() {
  float s = vS;
  float ad = vA - stringAt(s).x;
  vec3 sum = vec3(0.0);
  if (ad > 0.0 && ad < uAur.y) {
    float fl = 0.0;
    for (int i = 0; i < int(uLive.y); i++) {
      float db = (s - uBump[i].x) / (uBump[i].y * 1.6);
      fl += uBump[i].z * exp(-0.5 * db * db);
    }
    fl = min(fl, 1.0);
    sum = sheet(s, ad, 0.0, uAur.x, fl);
    if (uAur.z > 1.5) sum += 0.6 * sheet(s, ad, 1.0, uAur.x, fl);
  }
  outColor = vec4(sum, 1.0);
}
`

// ----------------------------------------------------------------------------------------------------- stars
export const STAR_VERTEX = /* glsl */ `${HEAD}${TOOLS}${GEOMETRY}${SKY}
layout(location = 0) in vec3 aDir;     // the star's direction (J2000)
layout(location = 1) in float aMag;    // visual magnitude
layout(location = 2) in vec3 aCol;     // its colour, linear, at a luminance of 1
layout(location = 3) in float aBoost;  // brightened by a memory recalled (a share of its light added)
uniform vec4 uStar;     // the Gaussian's deviation (CSS px), the exposure, the wings' share and their width
uniform float uCut;     // the light below which a star is nothing
uniform float uTime;
uniform float uTwinkle; // 1, or 0 where nothing may move
flat out vec3 vLight;
flat out vec2 vPos;

void main() {
  vec3 q = aDir * uViewInv;
  vec2 corner = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1)) * 2.0 - 1.0;
  gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
  if (q.z < -0.995) return;
  vec2 pos = place(q);
  float alt = altitude(pos);
  uint id = uint(gl_InstanceID);
  // near the limb a star shines through more air: dimmer, redder, and twinkling hard
  float low = exp(-max(alt, 0.0) / (0.14 * uLimb.w));
  float h0 = rnd(id * 3u);
  float h1 = rnd(id * 3u + 1u);
  float h2 = rnd(id * 3u + 2u);
  float n1 = sin(uTime * (4.1 + 3.7 * h0) + h1 * TAU) * sin(uTime * (1.7 + 2.1 * h2) + h2 * TAU);
  float n2 = sin(uTime * (3.3 + 2.9 * h1) + h0 * TAU) * sin(uTime * (2.3 + 1.9 * h0) + h1 * TAU);
  float amp = (0.035 + 0.5 * low) * uTwinkle;
  vec3 tint = vec3(1.0 + 0.55 * amp * n2, 1.0, 1.0 - 0.55 * amp * n2);
  vec3 ext = exp(-vec3(1.0, 1.9, 3.9) * 1.7 * exp(-max(alt, 0.0) / 14.0));
  float flux = pow(10.0, -0.4 * (aMag - 6.5)) * (1.0 + aBoost);
  vec3 light = aCol * tint * ext * (uStar.y * flux * (1.0 + amp * n1));
  float peak = max(light.r, max(light.g, light.b));
  if (peak < uCut) return;
  // how far from its centre it still shows
  float core = uStar.x * sqrt(2.0 * log(max(1.0001, peak / uCut)));
  float wing = peak * uStar.z;
  float wings = wing > uCut ? uStar.w * sqrt(pow(wing / uCut, 2.0 / 3.0) - 1.0) : 0.0;
  float reach = max(core, wings) + 1.0;
  if (alt < -reach || pos.x < -reach || pos.x > uCss.x + reach || pos.y < -reach) return;
  vLight = light;
  vPos = pos;
  gl_Position = clipPos(pos + corner * reach);
}
`

export const STAR_FRAGMENT = /* glsl */ `${HEAD}${TOOLS}${GEOMETRY}${FRAG}
uniform vec4 uStar;
flat in vec3 vLight;
flat in vec2 vPos;
out vec4 outColor;

void main() {
  vec2 p = cssPos();
  vec2 d = p - vPos;
  vec2 hw = 0.5 / uPx;
  float sg = uStar.x;
  // the Gaussian's light averaged over this pixel (its share of the star's, over the pixel's area), then the wings
  float dens = (cdf((d.x + hw.x) / sg) - cdf((d.x - hw.x) / sg)) * (cdf((d.y + hw.y) / sg) - cdf((d.y - hw.y) / sg))
    * (2.0 * PI * sg * sg) * (uPx.x * uPx.y);
  float wing = uStar.z / pow(1.0 + dot(d, d) / (uStar.w * uStar.w), 1.5);
  vec3 shown = 1.0 - exp(-vLight * (dens + wing));
  // behind the world's edge
  float cover = clamp(altitude(p) * 0.5 * (uPx.x + uPx.y) + 0.5, 0.0, 1.0);
  outColor = vec4(encode(shown * cover), 1.0);
}
`

// ----------------------------------------------------------------------------------------------------- strokes
/** A stroke's quad round its segment, from the pen's writing (screen points) or the figure (two stars). */
const STROKE_QUAD = /* glsl */ `
uniform float uMargin;
flat out vec4 vSeg;
flat out vec4 vPen;
out vec2 vP;
void quad(vec2 p0, vec2 p1, vec4 pen) {
  vec2 d = p1 - p0;
  float len = length(d);
  vec2 t = len > 1e-4 ? d / len : vec2(1.0, 0.0);
  vec2 n = vec2(-t.y, t.x);
  vec2 c = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1)) * 2.0 - 1.0;
  vec2 pos = mix(p0, p1, c.x * 0.5 + 0.5) + t * (c.x * uMargin) + n * (c.y * uMargin);
  vSeg = vec4(p0, p1);
  vPen = pen;
  vP = pos;
  gl_Position = clipPos(pos);
}
`

export const WRITE_VERTEX = /* glsl */ `${HEAD}${GEOMETRY}${STROKE_QUAD}
layout(location = 0) in vec4 aSeg;  // x0, y0, x1, y1: CSS px from the writing's origin
layout(location = 1) in vec4 aPen;  // when the pen starts and ends the segment (s), the ink at each end
uniform vec2 uOrigin;
void main() { quad(aSeg.xy + uOrigin, aSeg.zw + uOrigin, aPen); }
`

export const FIGURE_VERTEX = /* glsl */ `${HEAD}${GEOMETRY}${SKY}${STROKE_QUAD}
layout(location = 0) in vec3 aA;     // the line's two stars
layout(location = 1) in vec3 aB;
layout(location = 2) in vec2 aTime;  // when it starts drawing, and how long it takes (s)
uniform float uGap;                  // the line stops this short of each star (CSS px)
void main() {
  vec2 p0 = place(aA * uViewInv);
  vec2 p1 = place(aB * uViewInv);
  vec2 t = normalize(p1 - p0 + 1e-4);
  quad(p0 + t * uGap, p1 - t * uGap, vec4(aTime.x, aTime.x + aTime.y, 1.0, 1.0));
}
`

export const STROKE_FRAGMENT = /* glsl */ `${HEAD}${TOOLS}
uniform vec2 uPx;
uniform float uT;       // the pen's clock (s)
uniform vec3 uColour;
uniform vec4 uStroke;   // the core's deviation (CSS px), the halo's strength and its scale (CSS px), the light's fade
uniform vec3 uInk;      // wet ink's extra light, how long it takes to settle (s), the pen tip's light
flat in vec4 vSeg;
flat in vec4 vPen;
in vec2 vP;
out vec4 outColor;

void main() {
  float t0 = vPen.x;
  float t1 = vPen.y;
  if (uT < t0) discard;
  // the part of the segment the pen has drawn, with a round end where it is now
  float f = clamp((uT - t0) / max(t1 - t0, 1e-4), 0.0, 1.0);
  vec2 p0 = vSeg.xy;
  vec2 d = vSeg.zw - p0;
  float dd = dot(d, d);
  float h = dd > 1e-6 ? clamp(dot(vP - p0, d) / dd, 0.0, f) : 0.0;
  float r = length(vP - (p0 + d * h));
  float age = max(uT - (t0 + h * (t1 - t0)), 0.0);
  float ink = mix(vPen.z, vPen.w, h);
  float w = 0.5 * (1.0 / uPx.x + 1.0 / uPx.y);
  float core = profile(r, uStroke.x, w) * 2.5066 * uStroke.x;
  float halo = uStroke.y * exp(-r / uStroke.z);
  float wet = 1.0 + uInk.x * exp(-age / uInk.y);
  vec2 tipAt = vP - (p0 + d * f);
  float tip = uT <= t1 + 0.02 ? uInk.z * exp(-0.5 * dot(tipAt, tipAt) / 6.76) : 0.0;
  outColor = vec4(encode(uColour * ((ink * wet * (core + halo) + tip) * uStroke.w)), 1.0);
}
`

// ----------------------------------------------------------------------------------------------------- meteors
export const METEOR_VERTEX = /* glsl */ `${HEAD}${GEOMETRY}
layout(location = 0) in vec4 aPath;    // from (the limb) and to (a card), CSS px
layout(location = 1) in vec2 aAge;     // seconds since it left, and how long the flight takes
layout(location = 2) in vec3 aColour;
uniform vec2 uTrail;                   // the tail's length in time (s), how long it takes to fade after arriving
const int N = 36;
out float vAcross;
out float vTau;
out float vHead;
out float vFade;
out vec3 vColour;

float ease(float x) { return 1.0 - pow(1.0 - clamp(x, 0.0, 1.0), 2.4); }

void main() {
  int i = gl_VertexID >> 1;
  float side = (gl_VertexID & 1) == 0 ? -1.0 : 1.0;
  float tau = float(i) / float(N);
  float age = aAge.x;
  float D = aAge.y;
  float tm = clamp(age - tau * uTrail.x, 0.0, D);
  vec2 pos = mix(aPath.xy, aPath.zw, ease(tm / D));
  vec2 head = mix(aPath.xy, aPath.zw, ease(age / D));
  vec2 t = normalize(aPath.zw - aPath.xy + 1e-4);
  if (i == 0) pos += t * 7.0;   // the ribbon runs a little past the head, so its glow falls off round, not cut
  float width = 4.5 * (1.0 - 0.5 * tau);
  vAcross = side * width;
  vTau = tau;
  vHead = length(pos - head);
  vFade = 1.0 - smoothstep(D, D + uTrail.y, age);
  vColour = aColour;
  gl_Position = clipPos(pos + vec2(-t.y, t.x) * (side * width));
}
`

export const METEOR_FRAGMENT = /* glsl */ `${HEAD}${TOOLS}
in float vAcross;
in float vTau;
in float vHead;
in float vFade;
in vec3 vColour;
out vec4 outColor;

void main() {
  float x = vAcross;
  float body = exp(-x * x / 0.5) + 0.28 * exp(-abs(x) / 1.4);
  float head = 1.6 * exp(-0.5 * vHead * vHead / 4.84) + 0.25 * exp(-vHead / 9.0);
  float light = (body * pow(1.0 - vTau, 1.7) + head * exp(-x * x / 6.0)) * vFade;
  outColor = vec4(encode(mix(vColour, vec3(1.0), 0.65 * (1.0 - vTau)) * light), 1.0);
}
`
