// The miniature company world: an isometric SVG drawn entirely from the tenant's layout config, and a
// visual layer that only reflects real system events. Nothing here creates or decides business work.

const TW = 56, TH = 28, HZ = 30;
const NS = 'http://www.w3.org/2000/svg';
export const P = (x, y, z = 0) => [(x - y) * TW / 2, (x + y) * TH / 2 - z * HZ];
const pts = (a) => a.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
const poly = (a, fill, extra = '') => `<polygon points="${pts(a)}" fill="${fill}" stroke="${fill}" stroke-width="1.1" stroke-linejoin="round" ${extra}/>`;
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
let gidSeq = 0;

function box(x, y, w, d, h, z0, c) {
  const z1 = z0 + h;
  const B0 = P(x + w, y, z0), C0 = P(x + w, y + d, z0), D0 = P(x, y + d, z0);
  const A1 = P(x, y, z1), B1 = P(x + w, y, z1), C1 = P(x + w, y + d, z1), D1 = P(x, y + d, z1);
  return poly([D0, C0, C1, D1], c[1]) + poly([B0, C0, C1, B1], c[2]) + poly([A1, B1, C1, D1], c[0]) +
    `<polyline points="${pts([D1, C1, B1])}" fill="none" stroke="rgba(255,255,255,.6)" stroke-width="1.1" stroke-linejoin="round"/>`;
}
const fy = (x, y, w, d, z0, h) => (u, v) => P(x + u * w, y + d, z0 + v * h);
const fx = (x, y, w, d, z0, h) => (u, v) => P(x + w, y + u * d, z0 + v * h);
const quad = (f, u0, u1, v0, v1, fill, extra = '') => poly([f(u0, v0), f(u1, v0), f(u1, v1), f(u0, v1)], fill, extra);
const line = (a, b, stroke, w = 1) => `<polyline points="${pts([a, b])}" stroke="${stroke}" stroke-width="${w}" fill="none"/>`;
function faceText(f, u, v, txt, size, fill, dir, weight = 700, ls = '.06em') {
  const [px, py] = f(u, v);
  const m = dir === 'y' ? `matrix(1,0.5,0,1,${px.toFixed(1)},${py.toFixed(1)})` : `matrix(1,-0.5,0,1,${px.toFixed(1)},${py.toFixed(1)})`;
  return `<text transform="${m}" text-anchor="middle" font-family="Sora,Segoe UI,sans-serif" font-weight="${weight}" font-size="${size}" letter-spacing="${ls}" fill="${fill}">${esc(txt)}</text>`;
}
function cyl(x, y, z0, r, h, light, dark, top, band) {
  const [cx, cb] = P(x, y, z0), [, ct] = P(x, y, z0 + h);
  const rx = r * TW * 0.7071, ry = r * TH * 0.7071, gid = 'cg' + (++gidSeq);
  const f = (n) => n.toFixed(1);
  let s = `<defs><linearGradient id="${gid}" x1="0" x2="1"><stop offset="0" stop-color="${light}"/><stop offset="1" stop-color="${dark}"/></linearGradient></defs>`;
  s += `<path d="M${f(cx - rx)} ${f(ct)} L${f(cx - rx)} ${f(cb)} A${f(rx)} ${f(ry)} 0 0 0 ${f(cx + rx)} ${f(cb)} L${f(cx + rx)} ${f(ct)} A${f(rx)} ${f(ry)} 0 0 1 ${f(cx - rx)} ${f(ct)}Z" fill="url(#${gid})"/>`;
  if (band) { const yb = ct + (cb - ct) * 0.45; s += `<path d="M${f(cx - rx)} ${f(yb)} A${f(rx)} ${f(ry)} 0 0 0 ${f(cx + rx)} ${f(yb)}" fill="none" stroke="${band}" stroke-width="3"/>`; }
  return s + `<ellipse cx="${f(cx)}" cy="${f(ct)}" rx="${f(rx)}" ry="${f(ry)}" fill="${top}"/>`;
}
const ground = (x0, y0, x1, y1, fill, sw = 1.2) => `<polygon points="${pts([P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)])}" fill="${fill}" stroke="${fill}" stroke-width="${sw}" stroke-linejoin="round"/>`;
const fan = (x, y, z) => { const [cx, cy] = P(x, y, z); return `<ellipse cx="${cx}" cy="${cy}" rx="9" ry="4.5" fill="#C9D6F1"/>`; };

const COL = {
  blue: ['#B2C8FC', '#729AF2', '#5D84E4'], blueSoft: ['#BCD0FD', '#86A9F6', '#7398EE'],
  white: ['#F6F8FF', '#E9EFFC', '#D6E0F6'], glass: ['#C6D7FE', '#86ABF8', '#6E95EE'],
  orange: ['#FFB76A', '#F59A3C', '#DF8128'], wood: ['#F7C58C', '#E9A35C', '#D58B44'],
  navy: ['#5B6F9E', '#2E4170', '#22335C'], pallet: ['#E2C6A0', '#C9A57A', '#B48E62'],
};
const signSize = (w, txt, max) => Math.min(max, (w * 28) / Math.max(4, String(txt).length * 0.66)); // fit the sign to its face

// ---------------------------------------------------------------- building templates
const BUILDINGS = {
  warehouse(x, y, w, d, h, sign) {
    const L = fy(x, y, w, d, 0, h), R = fx(x, y, w, d, 0, h);
    let s = box(x, y, w, d, h, 0, COL.blue);
    if (d > 2) s += poly([P(x + 0.7, y + 0.9, h), P(x + w - 0.6, y + 0.9, h), P(x + w - 0.6, y + 1.4, h), P(x + 0.7, y + 1.4, h)], '#D6E3FF');
    s += box(x + 1, y + d - 1.3, 0.9, 0.8, 0.36, h, COL.white) + fan(x + 1.45, y + d - 0.9, h + 0.36);
    s += quad(L, 0.05, 0.53, 0.84, 0.92, '#DCE7FF');
    if (sign) s += faceText(L, 0.29, 0.6, sign, signSize(w * 0.5, sign, 11.5), '#FFFFFF', 'y');
    for (const [u0, u1] of [[0.6, 0.76], [0.8, 0.96]]) {
      s += quad(L, u0, u1, 0, 0.64, '#3D63D1') + quad(L, u0 + 0.016, u1 - 0.016, 0, 0.58, '#FFB463') + quad(L, u0 + 0.016, u1 - 0.016, 0.34, 0.58, '#EDF2FF');
      for (const v of [0.4, 0.46, 0.52]) s += line(L(u0 + 0.016, v), L(u1 - 0.016, v), '#C3D2F3');
    }
    s += quad(R, 0.1, 0.32, 0.5, 0.75, '#DCE7FF') + quad(R, 0.38, 0.6, 0.5, 0.75, '#DCE7FF');
    return s + quad(R, 0.8, 0.92, 0, 0.46, '#2D57D3') + quad(R, 0.82, 0.9, 0.22, 0.4, '#BFD2FF');
  },
  factory(x, y, w, d, h, sign) {
    const L = fy(x, y, w, d, 0, h), R = fx(x, y, w, d, 0, h);
    let s = box(x, y, w, d, h, 0, COL.blue);
    s += box(x + 0.5, y + 0.4, w * 0.52, d * 0.38, 0.45, h, COL.orange);
    const stacks = [[x + 1.1, y + 1.05], [x + 2.25, y + 1.05]];
    for (const [sx, sy] of stacks) s += cyl(sx, sy, h + 0.45, 0.28, 1.7, '#D7E3FF', '#7FA2F2', '#5E7FC9', '#F59A3C');
    const rows = Math.max(1, Math.floor((d - 0.8) / 0.95));
    for (let i = 0; i < rows; i++) {
      const py = y + 0.5 + i * 0.95, x0 = x + w - 1.65, x1 = x + w - 0.3;
      s += poly([P(x0, py, h + 0.06), P(x1, py, h + 0.06), P(x1, py + 0.7, h + 0.06), P(x0, py + 0.7, h + 0.06)], '#3E62C8');
      s += line(P((x0 + x1) / 2, py, h + 0.06), P((x0 + x1) / 2, py + 0.7, h + 0.06), 'rgba(255,255,255,.35)');
      s += line(P(x0, py + 0.35, h + 0.06), P(x1, py + 0.35, h + 0.06), 'rgba(255,255,255,.35)');
    }
    if (sign) s += faceText(L, 0.27, 0.62, sign, signSize(w * 0.45, sign, 13), '#FFFFFF', 'y');
    const [gx, gy] = L(0.5, 0.68);
    s += `<g transform="translate(${gx.toFixed(1)},${gy.toFixed(1)})"><circle r="6" fill="#F59A3C"/><circle r="2.4" fill="#729AF2"/><circle cx="8" cy="5" r="4" fill="#F59A3C"/><circle cx="8" cy="5" r="1.5" fill="#729AF2"/></g>`;
    for (const [a, b] of [[0.05, 0.17], [0.21, 0.33], [0.37, 0.49], [0.86, 0.96]]) s += quad(L, a, b, 0.16, 0.4, '#DCE7FF');
    s += quad(L, 0.66, 0.82, 0, 0.54, '#E0822A') + quad(L, 0.675, 0.805, 0, 0.5, '#FFD3A3');
    for (const v of [0.36, 0.42]) s += line(L(0.675, v), L(0.805, v), '#F2B57A');
    for (const [a, b] of [[0.12, 0.3], [0.4, 0.58], [0.68, 0.86]]) s += quad(R, a, b, 0.45, 0.72, '#DCE7FF');
    const tops = stacks.map(([sx, sy]) => P(sx, sy, h + 2.15));
    return s + `<g class="smoke"><circle cx="${tops[0][0]}" cy="${tops[0][1] - 6}" r="6" fill="#B9CEFB"/><circle cx="${tops[0][0] + 2}" cy="${tops[0][1] - 8}" r="5" fill="#A7C1FA"/><circle cx="${tops[1][0]}" cy="${tops[1][1] - 6}" r="6" fill="#B9CEFB"/></g>`;
  },
  tower(x, y, w, d, h, sign) {
    const hb = Math.min(1.6, h * 0.32);
    const L = fy(x, y, w, d, 0, hb), R = fx(x, y, w, d, 0, hb);
    let s = box(x, y, w, d, hb, 0, COL.white);
    s += quad(L, 0.05, 0.35, 0.06, 0.6, '#BFD4FF') + quad(L, 0.39, 0.53, 0, 0.66, '#3D63D1') + quad(L, 0.41, 0.51, 0.08, 0.6, '#9DBBFB') + quad(L, 0.57, 0.95, 0.06, 0.6, '#BFD4FF');
    const [rx, ry] = L(0.2, 0.12);
    s += `<g transform="translate(${rx.toFixed(1)},${ry.toFixed(1)})"><circle cy="-15" r="3" fill="#F1CBA6"/><rect x="-3.5" y="-12" width="7" height="7" rx="2.5" fill="#22396B"/><rect x="-8" y="-6" width="16" height="5" rx="1" fill="#F59A3C"/></g>`;
    if (sign) s += faceText(L, 0.5, 0.71, sign, signSize(w * 0.9, sign, 9.5), '#E0822A', 'y', 700, '.08em');
    s += quad(R, 0.08, 0.92, 0.1, 0.58, '#BFD4FF');
    const gx = x + 0.25, gy = y + 0.25, gw = w - 0.5, gd = d - 0.5, gh = h - hb;
    const GL = fy(gx, gy, gw, gd, hb, gh), GR = fx(gx, gy, gw, gd, hb, gh);
    s += box(gx, gy, gw, gd, gh, hb, COL.glass);
    for (const u of [0.2, 0.4, 0.6, 0.8]) s += line(GL(u, 0), GL(u, 1), 'rgba(255,255,255,.5)', 1.2) + line(GR(u, 0), GR(u, 1), 'rgba(255,255,255,.4)', 1.2);
    for (const v of [0.34, 0.67]) s += line(GL(0, v), GL(1, v), 'rgba(255,255,255,.6)', 1.6) + line(GR(0, v), GR(1, v), 'rgba(255,255,255,.5)', 1.6);
    s += poly([GL(0.06, 0.4), GL(0.5, 0.4), GL(0.32, 0.62), GL(0.06, 0.62)], 'rgba(255,255,255,.18)');
    s += box(gx - 0.07, gy - 0.07, gw + 0.14, gd + 0.14, 0.14, h, COL.white);
    return s + box(x + 1.1, y + 0.8, 0.9, 0.8, 0.36, h + 0.14, COL.white) + fan(x + 1.55, y + 1.2, h + 0.5);
  },
  store(x, y, w, d, h, sign) {
    const L = fy(x, y, w, d, 0, h), R = fx(x, y, w, d, 0, h);
    let s = box(x, y, w, d, h, 0, ['#A6C0FB', '#EEF3FE', '#DAE3F8']);
    for (const [a, b] of [[0.06, 0.31], [0.37, 0.63], [0.69, 0.94]]) s += quad(L, a, b, 0.06, 0.44, '#CFE0FF');
    s += poly([L(0.03, 0.66), L(0.97, 0.66), P(x + 0.97 * w, y + d + 0.42, 0.5 * h), P(x + 0.03 * w, y + d + 0.42, 0.5 * h)], '#F59A3C');
    for (let k = 0; k < 6; k++) {
      const u0 = 0.03 + k * (0.94 / 6), u1 = u0 + 0.94 / 12;
      s += poly([L(u0, 0.66), L(u1, 0.66), P(x + u1 * w, y + d + 0.42, 0.5 * h), P(x + u0 * w, y + d + 0.42, 0.5 * h)], '#FFD3A3', 'stroke-width="0.4"');
    }
    if (sign) s += faceText(L, 0.5, 0.76, sign, signSize(w * 0.8, sign, 10.5), '#E0822A', 'y', 800, '.14em');
    s += quad(R, 0.38, 0.62, 0, 0.56, '#4A79F0') + quad(R, 0.41, 0.59, 0.08, 0.5, '#BFD4FF');
    return s + box(x - 0.65, y + d - 0.9, 0.42, 0.42, 0.32, 0, COL.orange) + box(x - 0.6, y + d - 0.38, 0.36, 0.36, 0.26, 0, COL.wood);
  },
  office(x, y, w, d, h, sign) {
    const L = fy(x, y, w, d, 0, h), R = fx(x, y, w, d, 0, h);
    let s = box(x, y, w, d, h, 0, COL.blueSoft) + box(x + 0.4, y + 0.3, 0.7, 0.6, 0.3, h, COL.white);
    if (sign) s += faceText(L, 0.5, 0.66, sign, signSize(w * 0.85, sign, 8.5), '#FFFFFF', 'y', 700, '.08em');
    s += quad(L, 0.43, 0.57, 0, 0.5, '#2D57D3') + quad(L, 0.45, 0.55, 0.18, 0.44, '#BFD4FF');
    s += quad(L, 0.07, 0.33, 0.14, 0.44, '#DCE7FF') + quad(L, 0.67, 0.93, 0.14, 0.44, '#DCE7FF');
    return s + quad(R, 0.12, 0.42, 0.3, 0.62, '#DCE7FF') + quad(R, 0.55, 0.85, 0.3, 0.62, '#DCE7FF');
  },
  pavilion(x, y, w, d, _h, sign, id) {
    let s = box(x, y, w, d, 0.14, 0, COL.white);
    const posts = [[x + 0.1, y + 0.1], [x + w - 0.22, y + 0.1], [x + 0.1, y + d - 0.22], [x + w - 0.22, y + d - 0.22]];
    for (const [px, py] of posts.slice(0, 2)) s += box(px, py, 0.12, 0.12, 1.75, 0.14, COL.white);
    s += box(x + w / 2 - 0.15, y + 0.35, 0.42, 0.38, 0.42, 0.14, COL.navy);
    s += box(x + w / 2 - 0.8, y + 0.75, 1.6, 0.55, 0.44, 0.14, COL.orange);
    const [dx, dy] = P(x + w / 2, y + 1.02, 0.58);
    s += `<rect x="${dx - 6}" y="${dy - 10}" width="12" height="8" rx="1.5" fill="#22396B"/><rect x="${dx - 5}" y="${dy - 9}" width="10" height="6" rx="1" fill="#9DBBFB"/>`;
    for (const [px, py] of posts.slice(2)) s += box(px, py, 0.12, 0.12, 1.75, 0.14, COL.white);
    s += box(x - 0.1, y - 0.1, w + 0.2, d + 0.2, 0.3, 1.89, COL.blue);
    if (sign) s += faceText(fy(x - 0.1, y - 0.1, w + 0.2, d + 0.2, 1.89, 0.3), 0.5, 0.14, sign, 7, '#FFFFFF', 'y', 700, '.12em');
    const [bx, by] = P(x + w / 2, y + d / 2, 2.6);
    return s + `<g class="beacon" data-kind="area" data-id="${esc(id)}" transform="translate(${bx.toFixed(1)},${by.toFixed(1)})"><circle class="ring" r="9" fill="none" stroke="#F28C28" stroke-width="2"/><circle r="7" fill="#F28C28"/><path d="M0 -3.5 V1" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cy="3.6" r="1.2" fill="#fff"/></g>`;
  },
};

// ---------------------------------------------------------------- props
function tree(x, y, s = 1) {
  const [cx, cy] = P(x, y), h = 30 * s, r = 9 * s, f = (n) => n.toFixed(1);
  return `<g><ellipse cx="${cx}" cy="${cy + 1}" rx="${10 * s}" ry="${4.5 * s}" fill="rgba(24,38,74,.10)"/><rect x="${f(cx - 1.6 * s)}" y="${f(cy - 8 * s)}" width="${f(3.2 * s)}" height="${f(8 * s)}" rx="1" fill="#6E83B4"/>` +
    `<polygon points="${f(cx - r)},${f(cy - 6 * s)} ${f(cx)},${f(cy - 6 * s - h)} ${f(cx)},${f(cy - 3 * s)}" fill="#4CBBA6"/><polygon points="${f(cx + r)},${f(cy - 6 * s)} ${f(cx)},${f(cy - 6 * s - h)} ${f(cx)},${f(cy - 3 * s)}" fill="#2F9E8C"/></g>`;
}
const TRUCK_CARGO = ['#A6C2FB', '#6F98F2', '#5A82E3'], TRUCK_CAB = ['#86A9F7', '#4A79F0', '#3C67DB'];
const wheel = ([x, y]) => `<ellipse cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" rx="3.3" ry="3.9" fill="#22335C"/><ellipse cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" rx="1.3" ry="1.5" fill="#9DB1DA"/>`;
// facing down-left (parked at a bay)
function truckMarkup() {
  let s = box(-0.42, -0.95, 0.84, 1.1, 0.95, 0.12, TRUCK_CARGO) + box(-0.4, 0.2, 0.8, 0.5, 0.7, 0.12, TRUCK_CAB);
  s += quad(fy(-0.4, 0.2, 0.8, 0.5, 0.12, 0.7), 0.12, 0.88, 0.48, 0.88, '#DCE8FF');
  return s + [P(0.42, -0.7, 0.12), P(0.42, -0.05, 0.12), P(0.4, 0.5, 0.12)].map(wheel).join('');
}
// facing down-right (driving along the main road)
function truckRoadMarkup() {
  let s = box(-0.95, -0.42, 1.1, 0.84, 0.95, 0.12, TRUCK_CARGO) + box(0.2, -0.4, 0.5, 0.8, 0.7, 0.12, TRUCK_CAB);
  s += quad(fx(0.2, -0.4, 0.5, 0.8, 0.12, 0.7), 0.12, 0.88, 0.48, 0.88, '#DCE8FF');
  return s + [P(-0.7, 0.42, 0.12), P(-0.05, 0.42, 0.12), P(0.5, 0.4, 0.12)].map(wheel).join('');
}
const isVehicle = (p) => p.type === 'truck' && (p.area || p.dataset);
const PROPS = {
  tree: (x, y, p) => [[x - 0.3, y - 0.3, x + 0.3, y + 0.3], tree(x, y, p.size)],
  bush: (x, y) => { const [cx, cy] = P(x, y); return [[x - 0.25, y - 0.25, x + 0.25, y + 0.25], `<g><ellipse cx="${cx}" cy="${cy + 1}" rx="8" ry="3.5" fill="rgba(24,38,74,.10)"/><circle cx="${cx - 3}" cy="${cy - 5}" r="5.5" fill="#4CBBA6"/><circle cx="${cx + 3}" cy="${cy - 6}" r="6" fill="#36A893"/></g>`]; },
  lamp: (x, y) => { const [cx, cy] = P(x, y); return [[x - 0.1, y - 0.1, x + 0.1, y + 0.1], `<g><ellipse cx="${cx}" cy="${cy}" rx="3.5" ry="1.6" fill="rgba(24,38,74,.15)"/><rect x="${cx - 1}" y="${cy - 30}" width="2" height="30" rx="1" fill="#8EA6D8"/><circle cx="${cx}" cy="${cy - 31}" r="3.4" fill="#FFFFFF" stroke="#8EA6D8" stroke-width="1.2"/></g>`]; },
  staff: (x, y) => {
    const [cx, cy] = P(x, y);
    return [[x - 0.15, y - 0.15, x + 0.15, y + 0.15], `<g opacity=".92"><ellipse cx="${cx}" cy="${cy}" rx="5.5" ry="2.6" fill="rgba(24,38,74,.14)"/><rect x="${cx - 3}" y="${cy - 6}" width="2.5" height="6" rx="1.2" fill="#55688F"/><rect x="${cx + 0.5}" y="${cy - 6}" width="2.5" height="6" rx="1.2" fill="#55688F"/>` +
      `<rect x="${cx - 4.3}" y="${cy - 14.5}" width="8.6" height="9.5" rx="3.5" fill="#A8BBDF"/><circle cx="${cx}" cy="${cy - 17.8}" r="3.6" fill="#F1CBA6"/><path d="M${cx - 3.7} ${cy - 18.2} a3.7 3.7 0 0 1 7.4 0z" fill="#7C90BC"/><rect x="${cx + 1}" y="${cy - 14}" width="7" height="6" rx="1" fill="#F5A24D"/></g>`];
  },
  pallets: (x, y) => {
    let s = '';
    [[0, 0.6], [0.62, 0.6], [0, 1.22], [0.62, 1.22], [0, 0]].forEach(([ox, oy], i) => {
      s += box(x + ox, y + oy, 0.55, 0.55, 0.08, 0, COL.pallet) + box(x + ox + 0.03, y + oy + 0.03, 0.49, 0.49, 0.42, 0.08, i % 2 ? COL.blueSoft : COL.orange);
      if (i < 2) s += box(x + ox + 0.06, y + oy + 0.06, 0.43, 0.43, 0.36, 0.5, i ? COL.orange : COL.blueSoft);
    });
    return [[x - 0.1, y - 0.1, x + 1.3, y + 1.8], s];
  },
  rack: (x, y, p) => {
    const x1 = x + 0.6, y1 = y + 2.6;
    let s = '';
    for (const py of [y, y1 - 0.1]) s += box(x, py, 0.08, 0.1, 1.25, 0, COL.navy) + box(x1 - 0.08, py, 0.08, 0.1, 1.25, 0, COL.navy);
    [0.05, 0.45, 0.85].forEach((z, i) => {
      s += box(x, y, 0.6, 2.6, 0.05, z, ['#D2DCF2', '#AFBEDE', '#9AABD0']);
      for (let k = 0; k < 5; k++) s += box(x + 0.1, y + 0.12 + k * 0.48, 0.4, 0.36, 0.28, z + 0.05, (k + i) % 3 === 0 ? COL.blueSoft : COL.wood);
    });
    if (p.label) s += faceText(fy(x, y, 0.6, 2.6, 0, 1.25), 0.5, 1.06, p.label, 6.5, '#2E4170', 'y', 700, '.04em');
    return [[x, y, x1 + 0.05, y1], s];
  },
  truck: (x, y) => { const [sx, sy] = P(x, y); return [[x - 0.45, y - 0.95, x + 0.45, y + 0.75], `<g transform="translate(${sx.toFixed(1)},${sy.toFixed(1)})"><ellipse rx="26" ry="12" fill="rgba(24,38,74,.14)"/>${truckMarkup()}</g>`]; },
  crates: (x, y) => [[x - 0.05, y - 0.05, x + 0.55, y + 0.5], box(x, y, 0.45, 0.45, 0.34, 0, COL.wood) + box(x + 0.05, y + 0.05, 0.36, 0.36, 0.28, 0.34, COL.wood)],
  tanks: (x, y) => {
    const out = [];
    for (let i = 0; i < 3; i++) {
      const ty = y + i * 1.3;
      let s = cyl(x, ty, 0, 0.45, 1.5, '#D2E0FF', '#6F96EE', '#C9D9FE', '#F59A3C');
      const a = P(x - 0.05, ty, 1.35), b = P(x - 0.6, ty, 1.9);
      s += `<path d="M${a[0].toFixed(1)} ${a[1].toFixed(1)} Q ${(a[0] - 4).toFixed(1)} ${(b[1] - 6).toFixed(1)} ${b[0].toFixed(1)} ${b[1].toFixed(1)}" fill="none" stroke="#F59A3C" stroke-width="3" stroke-linecap="round"/>`;
      out.push([[x - 0.5, ty - 0.5, x + 0.5, ty + 0.5], s]);
    }
    return out;
  },
};

function depthSort(objs) {
  const n = objs.length, sx = objs.map((o) => [(o.b[0] - o.b[3]) * TW / 2, (o.b[2] - o.b[1]) * TW / 2]);
  const before = (a, b) => a.b[2] <= b.b[0] + 1e-6 || a.b[3] <= b.b[1] + 1e-6;
  const adj = objs.map(() => []), indeg = new Array(n).fill(0);
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    if (!(sx[i][0] < sx[j][1] && sx[j][0] < sx[i][1])) continue;
    if (before(objs[i], objs[j])) { adj[i].push(j); indeg[j]++; } else if (before(objs[j], objs[i])) { adj[j].push(i); indeg[i]++; }
  }
  const key = (i) => objs[i].b.reduce((a, b) => a + b, 0), ready = [], out = [];
  for (let i = 0; i < n; i++) if (!indeg[i]) ready.push(i);
  while (ready.length) {
    ready.sort((a, b) => key(a) - key(b));
    const i = ready.shift(); out.push(i);
    for (const j of adj[i]) if (--indeg[j] === 0) ready.push(j);
  }
  for (let i = 0; i < n; i++) if (!out.includes(i)) out.push(i);
  return out.map((i) => objs[i]);
}

function buildScene(layout) {  // linked trucks are drawn in the live layer instead (they can be clicked and can drive)
  const [W, D] = layout.extent;
  const c = [P(-0.6, -0.6), P(W - 0.4, -0.6), P(W - 0.4, D + 0.1), P(-0.6, D + 0.1)], dn = (p) => [p[0], p[1] + 16];
  let g = `<defs><radialGradient id="gshadow"><stop offset="0" stop-color="#9FB3DA" stop-opacity=".45"/><stop offset="1" stop-color="#9FB3DA" stop-opacity="0"/></radialGradient></defs>`;
  const ctr = P(W / 2 - 0.5, D / 2 - 0.25);
  g += `<ellipse cx="${ctr[0]}" cy="${ctr[1] + 70}" rx="${W * 32}" ry="${D * 19}" fill="url(#gshadow)"/>`;
  g += `<polygon points="${pts([c[3], c[2], dn(c[2]), dn(c[3])])}" fill="#E1E9FA" stroke="#E1E9FA" stroke-width="16" stroke-linejoin="round"/>`;
  g += `<polygon points="${pts([c[1], c[2], dn(c[2]), dn(c[1])])}" fill="#CFDBF5" stroke="#CFDBF5" stroke-width="16" stroke-linejoin="round"/>`;
  g += `<polygon id="slab" points="${pts(c)}" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="16" stroke-linejoin="round"/>`;
  for (const r of layout.roads) {
    g += ground(...r, '#86A8F6', 3);
    const horizontal = r[2] - r[0] >= r[3] - r[1];
    const a = horizontal ? P(r[0] + 0.5, (r[1] + r[3]) / 2) : P((r[0] + r[2]) / 2, r[1] + 0.3);
    const b = horizontal ? P(r[2] - 0.5, (r[1] + r[3]) / 2) : P((r[0] + r[2]) / 2, r[3] - 0.3);
    g += `<polyline points="${pts([a, b])}" stroke="#FFFFFF" stroke-width="1.6" stroke-dasharray="7 8" opacity=".85" fill="none"/>`;
  }
  for (const w of layout.walkways) g += ground(...w, '#CAD9FC', 2);
  const objs = [];
  for (const [id, a] of Object.entries(layout.areas)) {
    const [x, y, w, d] = a.footprint;
    const tpl = BUILDINGS[a.building];
    const [dx, dy] = a.door;
    g += ground(Math.min(dx, x + w) - 0.35, Math.min(dy, y + d) - 0.1, Math.max(dx, x + w) + 0.35, Math.max(dy, y + d) + 0.35, '#DCE6FD', 2);
    objs.push({ b: [x - 0.1, y - 0.1, x + w + 0.1, y + d + (a.building === 'store' ? 0.42 : 0.1)], svg: `<g class="bld" data-kind="area" data-id="${esc(id)}">${tpl(x, y, w, d, a.height, a.sign, id)}</g>` });
  }
  for (const p of layout.props.filter((x) => !isVehicle(x))) {
    const made = PROPS[p.type](p.at[0], p.at[1], p);
    const list = Array.isArray(made[0][0]) ? made : [made];
    for (const [b, svg] of list) objs.push({ b, svg });
  }
  return `<g id="ground">${g}</g><g id="fx"></g><g id="static">${depthSort(objs).map((o) => o.svg).join('')}</g><g id="dyn"></g>`;
}

// ---------------------------------------------------------------- workers
export const ICONS = {
  search: '<circle cx="7" cy="7" r="4.2" fill="none" stroke="#2D57D3" stroke-width="2"/><path d="M10.2 10.2 L13.6 13.6" stroke="#2D57D3" stroke-width="2.2" stroke-linecap="round"/>',
  check: '<path d="M3.5 8.6 L6.6 11.6 L12.6 4.6" fill="none" stroke="#17875F" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>',
  error: '<path d="M4.5 4.5 L11.5 11.5 M11.5 4.5 L4.5 11.5" stroke="#C9443B" stroke-width="2.3" stroke-linecap="round"/>',
  alert: '<path d="M8 3 V9.2" stroke="#E0822A" stroke-width="2.4" stroke-linecap="round"/><circle cx="8" cy="12.4" r="1.4" fill="#E0822A"/>',
  talk: '<circle cx="4" cy="8" r="1.6" fill="#22396B"/><circle cx="8" cy="8" r="1.6" fill="#22396B"/><circle cx="12" cy="8" r="1.6" fill="#22396B"/>',
  doc: '<rect x="4" y="2.5" width="8" height="11" rx="1.4" fill="none" stroke="#22396B" stroke-width="1.8"/><path d="M6 6 H10 M6 8.5 H10 M6 11 H8.5" stroke="#22396B" stroke-width="1.4" stroke-linecap="round"/>',
  calc: '<rect x="3.5" y="2.5" width="9" height="11" rx="1.6" fill="none" stroke="#22396B" stroke-width="1.8"/><path d="M5.6 5.4 H10.4" stroke="#22396B" stroke-width="1.6"/><circle cx="6" cy="8.6" r=".9" fill="#22396B"/><circle cx="10" cy="8.6" r=".9" fill="#22396B"/><circle cx="6" cy="11.2" r=".9" fill="#22396B"/><circle cx="10" cy="11.2" r=".9" fill="#22396B"/>',
  shield: '<path d="M8 2.2 L13 4.2 V8 C13 11 10.8 13 8 14 C5.2 13 3 11 3 8 V4.2 Z" fill="#E3EBFE" stroke="#2D57D3" stroke-width="1.7" stroke-linejoin="round"/><path d="M5.8 8.2 L7.4 9.8 L10.4 6.6" fill="none" stroke="#2D57D3" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  pause: '<rect x="4.2" y="3.5" width="2.6" height="9" rx="1" fill="#5D6C8C"/><rect x="9.2" y="3.5" width="2.6" height="9" rx="1" fill="#5D6C8C"/>',
  hourglass: '<path d="M4.5 2.5 H11.5 M4.5 13.5 H11.5 M5.3 2.5 C5.3 6 10.7 6 10.7 8 C10.7 10 5.3 10 5.3 13.5 M10.7 2.5 C10.7 6 5.3 6 5.3 8 C5.3 10 10.7 10 10.7 13.5" fill="none" stroke="#E0822A" stroke-width="1.6" stroke-linecap="round"/>',
};
const shade = (hex, f) => '#' + [1, 3, 5].map((i) => Math.max(0, Math.min(255, Math.round(parseInt(hex.slice(i, i + 2), 16) * f))).toString(16).padStart(2, '0')).join('');

function workerMarkup(id, e) {
  const exec = !!e.answers_questions, dark = shade(e.color, 0.68);
  return `<g class="actor" data-kind="worker" data-id="${esc(id)}"><g transform="scale(1.45)">
    <circle r="17" cy="-12" fill="transparent"/>
    <ellipse class="sel-ring" rx="13" ry="6.5" fill="none" stroke="${e.color}" stroke-width="2.2"/>
    <ellipse rx="6.8" ry="3.3" fill="rgba(24,38,74,.17)"/>
    <g class="bob"><g class="idle-breathe"><g class="flip">
      <rect x="-3.7" y="-7.2" width="3.1" height="7.2" rx="1.5" fill="#2B3D66"/><rect x="0.6" y="-7.2" width="3.1" height="7.2" rx="1.5" fill="#2B3D66"/>
      <rect x="-5.3" y="-17" width="10.6" height="11.2" rx="4.2" fill="${e.color}"/>
      ${exec ? '<path d="M-1.6 -16.6 L0 -12.4 L1.6 -16.6 Z" fill="#FFFFFF"/><path d="M-0.7 -15 L0 -10.2 L0.7 -15 Z" fill="#F28C28"/>' : '<rect x="-5.3" y="-12.2" width="10.6" height="1.7" fill="rgba(255,255,255,.8)"/>'}
      <circle cy="-20.8" r="4.5" fill="#F3CDA8"/>
      ${exec ? `<path d="M-4.6 -21.4 a4.6 4.6 0 0 1 9.2 0 c-2 -1.2 -5 -1.6 -9.2 0z" fill="${dark}"/>` : `<path d="M-4.7 -21.2 a4.7 4.7 0 0 1 9.4 0 z" fill="${dark}"/><rect x="2.4" y="-22" width="4.3" height="1.6" rx=".8" fill="${dark}"/>`}
    </g></g></g>
    <circle class="sdot" cx="5.8" cy="-16" r="2.7" stroke="#FFFFFF" stroke-width="1.3" fill="#9AAAC8"/>
    <g class="bubble" transform="translate(0,-38)" visibility="hidden"><rect x="-11" y="-11" width="22" height="19" rx="7" fill="#FFFFFF" stroke="#D8E2F5"/><path d="M-3 7.5 L0 11.5 L3 7.5 Z" fill="#FFFFFF" stroke="#D8E2F5"/><rect x="-4" y="6" width="8" height="2.4" fill="#FFFFFF"/><g class="ic" transform="translate(-6.4,-8.6) scale(.8)"></g></g>
    <g class="tag" transform="translate(0,15)"><rect x="-38" y="-8" width="76" height="15" rx="7.5" fill="${e.color}"/><text y="3" text-anchor="middle" font-family="Manrope,Segoe UI,sans-serif" font-weight="800" font-size="8.5" fill="#FFFFFF">${esc(e.name)}</text></g>
  </g></g>`;
}

// ---------------------------------------------------------------- walk graph from layout paths
function buildGraph(layout, employees) {
  const nodes = {}, adj = {};
  const add = (k, p) => { if (!nodes[k]) { nodes[k] = p; adj[k] = []; } return k; };
  const link = (a, b) => { const d = Math.hypot(nodes[a][0] - nodes[b][0], nodes[a][1] - nodes[b][1]); adj[a].push([b, d]); adj[b].push([a, d]); };
  const vkey = (p) => `v:${p[0]},${p[1]}`;
  for (const path of layout.paths) {
    for (let i = 0; i < path.length; i++) {
      add(vkey(path[i]), path[i]);
      if (i) link(vkey(path[i - 1]), vkey(path[i]));
    }
  }
  const vertices = Object.keys(nodes);
  const nearest = (p) => vertices.reduce((best, k) => (Math.hypot(nodes[k][0] - p[0], nodes[k][1] - p[1]) < Math.hypot(nodes[best][0] - p[0], nodes[best][1] - p[1]) ? k : best), vertices[0]);
  const nodeArea = {};
  for (const [id, a] of Object.entries(layout.areas)) {
    const door = add(`door:${id}`, a.door); nodeArea[door] = id;
    if (vertices.length) link(door, nearest(a.door));
    for (const [name, p] of Object.entries(a.spots || {})) {
      const k = add(`spot:${id}:${name}`, p); nodeArea[k] = id;
      if (vertices.length) link(k, nearest(p));
    }
  }
  const offsets = [[0.1, 0.85], [0.85, 0.45], [-0.75, 0.55], [0.6, 1.1]], used = {};
  for (const [id, e] of Object.entries(employees)) {
    const a = layout.areas[e.area]; const n = used[e.area] = (used[e.area] || 0);
    used[e.area] = n + 1;
    const o = offsets[n % offsets.length];
    const k = add(`home:${id}`, [a.door[0] + o[0], a.door[1] + o[1]]); nodeArea[k] = e.area;
    link(k, `door:${e.area}`);
  }
  const route = (from, to) => {
    const dist = {}, prev = {}, left = new Set(Object.keys(nodes));
    for (const k of left) dist[k] = Infinity;
    dist[from] = 0;
    while (left.size) {
      let u = null;
      for (const k of left) if (u === null || dist[k] < dist[u]) u = k;
      left.delete(u);
      if (u === to || dist[u] === Infinity) break;
      for (const [v, d] of adj[u]) if (dist[u] + d < dist[v]) { dist[v] = dist[u] + d; prev[v] = u; }
    }
    const path = []; let c = to;
    while (c && c !== from) { path.unshift(c); c = prev[c]; }
    return c === from ? path : [to];
  };
  return { nodes, nodeArea, route };
}

// ---------------------------------------------------------------- the world
export function createWorld(svg, boot, onSelect) {
  const layout = boot.layout, employees = boot.employees;
  svg.innerHTML = buildScene(layout);
  const dyn = svg.querySelector('#dyn'), fxL = svg.querySelector('#fx');
  const vehicleProps = layout.props.filter(isVehicle);
  dyn.innerHTML = vehicleProps.map((p, i) => `<g class="vehicle" data-vehicle="${i}" ${p.area ? `data-kind="area" data-id="${esc(p.area)}" style="cursor:pointer"` : ''}><ellipse rx="26" ry="12" fill="rgba(24,38,74,.14)"/><g class="vy">${truckMarkup()}</g><g class="vx" visibility="hidden">${truckRoadMarkup()}</g></g>`).join('') +
    Object.entries(employees).map(([id, e]) => workerMarkup(id, e)).join('');
  // The main road is the widest horizontal road; trucks leave the campus along it.
  const mainRoad = [...layout.roads].filter((r) => r[2] - r[0] >= r[3] - r[1]).sort((a, b) => (b[2] - b[0]) - (a[2] - a[0]))[0];
  const vehicles = vehicleProps.map((p, i) => ({ p, el: dyn.querySelector(`[data-vehicle="${i}"]`), pos: [...p.at], busy: false }));
  const placeVehicle = (v, x, y, alongRoad, opacity = 1) => {
    const [sx, sy] = P(x, y);
    v.el.setAttribute('transform', `translate(${sx.toFixed(1)},${sy.toFixed(1)})`);
    v.el.querySelector('.vy').setAttribute('visibility', alongRoad ? 'hidden' : 'visible');
    v.el.querySelector('.vx').setAttribute('visibility', alongRoad ? 'visible' : 'hidden');
    v.el.style.opacity = opacity;
  };
  vehicles.forEach((v) => placeVehicle(v, v.p.at[0], v.p.at[1], false));
  /** A real delivery event: the truck pulls onto the main road, drives off the campus, then returns to its bay. */
  function drive(v) {
    if (v.busy || !mainRoad || paused || !enabled || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    v.busy = true;
    const roadY = (mainRoad[1] + mainRoad[3]) / 2, endX = layout.extent[0] + 1;
    const legs = [[v.p.at[0], roadY, false], [endX, roadY, true]];
    let [x, y] = v.p.at, leg = 0, last = performance.now();
    const step = (t) => {
      const dt = Math.min(60, t - last) / 1000; last = t;
      const [tx, ty, road] = legs[leg];
      const d = Math.hypot(tx - x, ty - y), move = 3.2 * dt;
      if (d <= move) { x = tx; y = ty; leg += 1; } else { x += (tx - x) / d * move; y += (ty - y) / d * move; }
      placeVehicle(v, x, y, road || leg > 0, x > layout.extent[0] - 2 ? Math.max(0, (layout.extent[0] + 1 - x) / 3) : 1);
      if (leg < legs.length) { requestAnimationFrame(step); return; }
      setTimeout(() => { placeVehicle(v, v.p.at[0], v.p.at[1], false, 0); v.el.style.transition = 'opacity .6s'; requestAnimationFrame(() => { v.el.style.opacity = 1; }); setTimeout(() => { v.el.style.transition = ''; v.busy = false; }, 700); }, 1500);
    };
    requestAnimationFrame(step);
  }
  const graph = buildGraph(layout, employees);
  const actors = {};
  for (const id of Object.keys(employees)) {
    const el = dyn.querySelector(`[data-id="${CSS.escape(id)}"]`);
    actors[id] = { id, el, node: `home:${id}`, pos: [...graph.nodes[`home:${id}`]], queue: [], op: null, wp: [], facing: 1, phase: 0, wait: null,
      bob: el.querySelector('.bob'), flip: el.querySelector('.flip'), bubble: el.querySelector('.bubble'), ic: el.querySelector('.ic'), sdot: el.querySelector('.sdot') };
  }
  let enabled = true, paused = false, running = false, last = 0, follow = null, userMoved = false;
  const cam = { x: 0, y: 0, w: 1200 }, camT = { x: 0, y: 0, w: 1200 };
  const listeners = new Set();
  const notify = () => listeners.forEach((f) => f());

  // camera
  function applyCam() {
    const r = svg.getBoundingClientRect(), ar = r.height && r.width ? r.height / r.width : 0.6, h = cam.w * ar;
    svg.setAttribute('viewBox', `${(cam.x - cam.w / 2).toFixed(1)} ${(cam.y - h / 2).toFixed(1)} ${cam.w.toFixed(1)} ${h.toFixed(1)}`);
  }
  function fit() {
    const r = svg.getBoundingClientRect(); if (!r.width || !r.height) return;
    const st = svg.querySelector('#static').getBBox(), sl = svg.querySelector('#slab').getBBox();
    const x0 = Math.min(st.x, sl.x) - 8, x1 = Math.max(st.x + st.width, sl.x + sl.width) + 8;
    const y0 = Math.min(st.y, sl.y) - 6, y1 = Math.max(st.y + st.height, sl.y + sl.height + 16) + 4;
    const top = r.width < 560 ? 60 : 54, bottom = r.width < 560 ? 12 : 44, availW = r.width - 24, availH = Math.max(120, r.height - top - bottom);
    const sc = Math.min(availW / (x1 - x0), availH / (y1 - y0)), offY = (top + availH / 2) - r.height / 2;
    camT.w = cam.w = r.width / sc; camT.x = cam.x = (x0 + x1) / 2; camT.y = cam.y = (y0 + y1) / 2 - offY / sc;
    userMoved = false; applyCam();
  }
  const camBusy = () => Math.abs(cam.x - camT.x) > 0.5 || Math.abs(cam.y - camT.y) > 0.5 || Math.abs(cam.w - camT.w) > 0.5;
  function focusGrid(x, y, w) { userMoved = true; const [sx, sy] = P(x, y, 0.8); camT.x = sx; camT.y = sy; camT.w = w || Math.min(camT.w, 760); kick(); }
  function zoomBy(f, cx, cy) {
    userMoved = true;
    const nw = Math.max(320, Math.min(2200, camT.w * f));
    if (cx !== undefined) { camT.x = cx + (camT.x - cx) * nw / camT.w; camT.y = cy + (camT.y - cy) * nw / camT.w; }
    camT.w = nw; kick();
  }

  // rendering
  function place(a) {
    const [sx, sy] = P(a.pos[0], a.pos[1]);
    a.el.setAttribute('transform', `translate(${sx.toFixed(1)},${sy.toFixed(1)})`);
    a.flip.setAttribute('transform', a.facing < 0 ? 'scale(-1,1)' : '');
    a.bob.setAttribute('transform', a.op && a.op.op === 'move' ? `translate(0,${(-Math.abs(Math.sin(a.phase)) * 1.7).toFixed(2)})` : '');
  }
  let lastOrder = '';
  function sortDyn() {
    const list = Object.values(actors).sort((a, b) => P(a.pos[0], a.pos[1])[1] - P(b.pos[0], b.pos[1])[1]);
    const key = list.map((a) => a.id).join();
    if (key !== lastOrder) { list.forEach((a) => dyn.appendChild(a.el)); lastOrder = key; }
  }
  function setBubble(a, icon) {
    if (!icon) { a.bubble.setAttribute('visibility', 'hidden'); return; }
    a.ic.innerHTML = ICONS[icon] || ''; a.bubble.setAttribute('visibility', 'visible');
  }

  // op queue
  function resolve(a, to) {
    if (to.home) return `home:${a.id}`;
    if (to.spot) return `spot:${to.area}:${to.spot}`;
    if (employees[a.id].area === to.area) return `home:${a.id}`; // already based there: stay at post
    return `door:${to.area}`;
  }
  function start(a) {
    if (a.op || !a.queue.length) return;
    const op = a.op = a.queue.shift();
    if (op.op === 'move') {
      const tgt = resolve(a, op.to);
      if (!graph.nodes[tgt] || tgt === a.node) { finish(a); return; }
      a.wp = graph.route(a.node, tgt).map((n) => ({ n, p: graph.nodes[n] })); a.wait = null; setBubble(a, null); op.target = tgt;
      notify();
    } else if (op.op === 'act') { op.t = op.ms; setBubble(a, op.icon); }
    else if (op.op === 'wait') { a.wait = op.icon; setBubble(a, op.icon); finish(a); }
  }
  function finish(a) {
    const op = a.op; a.op = null;
    if (op && op.op === 'act') setBubble(a, a.wait);
    if (op && op.op === 'move') notify();
    start(a);
  }
  const busy = () => Object.values(actors).some((a) => a.op || a.queue.length) || camBusy();
  function kick() { if (!running && !paused) { running = true; last = performance.now(); requestAnimationFrame(loop); } }
  function loop(t) {
    if (paused) { running = false; return; }
    const dt = Math.min(60, t - last); last = t;
    for (const a of Object.values(actors)) { if (!a.op) start(a); step(a, dt); }
    if (follow && actors[follow]) { const [fx2, fy2] = P(actors[follow].pos[0], actors[follow].pos[1]); camT.x = fx2; camT.y = fy2 - 20; }
    const k = 1 - Math.pow(0.0025, dt / 1000 * 3);
    cam.x += (camT.x - cam.x) * k; cam.y += (camT.y - cam.y) * k; cam.w += (camT.w - cam.w) * k;
    applyCam(); sortDyn(); updateLag();
    if (busy()) requestAnimationFrame(loop);
    else { running = false; Object.assign(cam, camT); applyCam(); }
  }
  function step(a, dt) {
    const op = a.op; if (!op) return;
    const f = 1 + Math.min(3, Math.max(0, a.queue.length - 1) * 0.7); // catch up when behind real events
    if (op.op === 'move') {
      let dist = 2.6 * f * dt / 1000;
      a.phase += dt * 0.016 * f;
      while (dist > 0 && a.wp.length) {
        const w = a.wp[0], dx = w.p[0] - a.pos[0], dy = w.p[1] - a.pos[1], len = Math.hypot(dx, dy);
        const sdx = dx - dy; if (Math.abs(sdx) > 0.01) a.facing = sdx > 0 ? 1 : -1;
        if (len <= dist) { a.pos = [...w.p]; dist -= len; a.node = w.n; a.wp.shift(); }
        else { a.pos = [a.pos[0] + dx / len * dist, a.pos[1] + dy / len * dist]; dist = 0; }
      }
      place(a);
      if (!a.wp.length) finish(a);
    } else if (op.op === 'act') { op.t -= dt * f; if (op.t <= 0) finish(a); }
    else finish(a);
  }
  function jump(a, op) { // visual layer off: keep the latest known position without animating
    if (op.op === 'move') { const n = resolve(a, op.to); if (graph.nodes[n]) { a.node = n; a.pos = [...graph.nodes[n]]; a.wait = null; } }
    else if (op.op === 'wait') a.wait = op.icon;
  }
  function flush() {
    for (const a of Object.values(actors)) {
      if (a.op && a.op.op === 'move') a.queue.unshift(a.op);
      a.op = null; a.wp = [];
      for (const op of a.queue) jump(a, op);
      a.queue = []; setBubble(a, a.wait); place(a);
    }
    sortDyn();
  }
  function pulse(area, tone) {
    if (!enabled || paused || !layout.areas[area]) return;
    const [x, y, w, d] = layout.areas[area].footprint, [cx, cy] = P(x + w / 2, y + d / 2);
    const e = document.createElementNS(NS, 'ellipse');
    e.setAttribute('cx', cx); e.setAttribute('cy', cy); e.setAttribute('rx', 120); e.setAttribute('ry', 60); e.setAttribute('class', 'pulse ' + tone);
    fxL.appendChild(e); e.addEventListener('animationend', () => e.remove()); setTimeout(() => e.remove(), 2500);
  }
  const lagEl = document.getElementById('lag');
  const queued = () => Object.values(actors).reduce((n, a) => n + a.queue.length, 0);
  function updateLag() {
    const q = queued();
    if (!enabled) { lagEl.hidden = true; return; }
    if (paused) { lagEl.hidden = false; lagEl.textContent = q ? `Visual paused · ${q} steps queued` : 'Visual paused'; return; }
    lagEl.hidden = q <= 3; lagEl.textContent = `Catching up · ${q} steps`;
  }

  // the visual adapter: one real event -> zero or more visual operations
  function toVisual(e) {
    const tool = e.tool && boot.tools[e.tool];
    const areaOf = (emp) => employees[emp] && employees[emp].area;
    switch (e.type) {
      case 'DATA_SYNC': return e.area ? [{ area: e.area, op: 'pulse', tone: 'blue' }] : [];
      case 'RULE_TRIGGERED': return e.area ? [{ area: e.area, op: 'pulse', tone: 'orange' }] : [];
      case 'WORK_ITEM_CREATED': return [{ actor: e.emp, op: 'act', icon: 'alert', ms: 900 }];
      case 'TOOL_CALLED': return tool ? [{ actor: e.emp, op: 'move', to: { area: tool.area, spot: tool.spot } }, { actor: e.emp, op: 'act', icon: 'search', ms: 1100 }] : [];
      case 'TOOL_RESULT': return [{ actor: e.emp, op: 'act', icon: 'check', ms: 700 }];
      case 'TOOL_ERROR': case 'ACTION_FAILED': return [{ actor: e.emp, op: 'act', icon: 'error', ms: 1200 }];
      case 'CALCULATION': return [{ actor: e.emp, op: 'act', icon: 'calc', ms: 800 }];
      case 'AI_REQUEST': return [{ actor: e.emp, op: 'move', to: { area: areaOf(e.to) } }, { actor: e.emp, op: 'act', icon: 'talk', ms: 700 }, { actor: e.to, op: 'act', icon: 'talk', ms: 700 }];
      case 'PRE_ACTION_CHECKS': return [{ actor: e.emp, op: 'act', icon: 'shield', ms: 900 }];
      case 'HANDOFF': return [{ actor: e.emp, op: 'move', to: { area: areaOf(e.to) } }, { actor: e.emp, op: 'act', icon: 'doc', ms: 700 }];
      case 'FINDING_RECEIVED': return [{ actor: e.emp, op: 'act', icon: 'doc', ms: 1200 }];
      case 'APPROVAL_REQUESTED': return [{ actor: e.emp, op: 'move', to: { area: e.area || 'approvals' } }, { actor: e.emp, op: 'wait', icon: 'hourglass' }];
      case 'HUMAN_DECISION': return [{ actor: e.emp, op: 'wait', icon: null }, { actor: e.emp, op: 'act', icon: e.decision === 'REJECTED' ? 'error' : 'check', ms: 900 }];
      case 'ACTION_EXECUTED': return [{ actor: e.emp, op: 'act', icon: 'doc', ms: 1200 }];
      case 'RESULT_VERIFIED': return [{ actor: e.emp, op: 'act', icon: 'check', ms: 800 }];
      case 'EMPLOYEE_PAUSED': return [{ actor: e.emp, op: 'wait', icon: 'pause' }];
      case 'EMPLOYEE_RESUMED': return [{ actor: e.emp, op: 'wait', icon: null }];
      case 'ROUTE_ESCALATED': return [{ actor: e.emp, op: 'act', icon: 'talk', ms: 900 }];
      case 'REPORT_GENERATED': return [{ actor: e.emp, op: 'act', icon: 'doc', ms: 1400 }];
      case 'STATE_CHANGED':
        if (e.status === 'VALIDATING') return [{ actor: e.emp, op: 'act', icon: 'shield', ms: 900 }];
        if ((e.status === 'COMPLETED' || e.status === 'FAILED') && !e.parent) return [{ actor: e.emp, op: 'move', to: { home: true } }];
        return [];
      default: return [];
    }
  }
  function describe(ops) {
    if (!ops.length) return { t: 'no visual', cls: '' };
    const mv = ops.find((o) => o.op === 'move');
    if (mv) {
      const name = (employees[mv.actor] || {}).name || mv.actor;
      const dest = mv.to.home ? 'home' : (layout.areas[mv.to.area] || {}).name || mv.to.area;
      return { t: `${name} → ${dest}`, cls: 'mv' };
    }
    const o = ops[0];
    if (o.op === 'pulse') return { t: `pulse · ${(layout.areas[o.area] || {}).name || o.area}`, cls: '' };
    return { t: `${(employees[o.actor] || {}).name || o.actor} · ${o.icon || 'ready'}`, cls: '' };
  }
  function consume(e) {
    if (e.type === 'DATA_SYNC' && e.new_rows) {
      const leaving = vehicles.filter((v) => v.p.dataset && (e.new_rows[v.p.dataset] || 0) > 0);
      leaving.forEach(drive);
      if (leaving.length) return { t: `truck leaves with ${e.new_rows[leaving[0].p.dataset]} new shipment${e.new_rows[leaving[0].p.dataset] === 1 ? '' : 's'}`, cls: 'mv' };
    }
    const ops = toVisual(e).filter((o) => o.area ? true : actors[o.actor]);
    for (const op of ops) {
      if (op.op === 'pulse') { pulse(op.area, op.tone); continue; }
      const a = actors[op.actor];
      if (!enabled) { jump(a, op); setBubble(a, a.wait); place(a); continue; }
      a.queue.push({ ...op });
    }
    if (enabled) kick();
    updateLag();
    if (ops.length) notify();
    return describe(ops);
  }

  // interaction
  let drag = null;
  svg.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, cx: camT.x, cy: camT.y, moved: false, id: e.pointerId }; });
  svg.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) > 5) { drag.moved = true; svg.classList.add('dragging'); try { svg.setPointerCapture(drag.id); } catch { /* ignore */ } follow = null; userMoved = true; notify(); }
    if (drag.moved) { const s = cam.w / svg.getBoundingClientRect().width; camT.x = cam.x = drag.cx - dx * s; camT.y = cam.y = drag.cy - dy * s; applyCam(); }
  });
  svg.addEventListener('pointerup', (e) => {
    if (drag && !drag.moved) {
      const t = e.target.closest('[data-kind]');
      if (t) onSelect(t.dataset.kind, t.dataset.id);
    }
    drag = null; svg.classList.remove('dragging');
  });
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect(), s = cam.w / r.width;
    zoomBy(e.deltaY > 0 ? 1.12 : 1 / 1.12, cam.x + (e.clientX - r.left - r.width / 2) * s, cam.y + (e.clientY - r.top - r.height / 2) * s);
  }, { passive: false });
  new ResizeObserver(() => { if (userMoved) applyCam(); else fit(); }).observe(svg);

  Object.values(actors).forEach(place); sortDyn();
  requestAnimationFrame(fit);

  return {
    consume, fit, zoomBy,
    onChange: (f) => listeners.add(f),
    get enabled() { return enabled; }, get paused() { return paused; }, get follow() { return follow; },
    setFollow(id) { follow = id; if (id) { camT.w = Math.min(camT.w, 720); kick(); } notify(); },
    setEnabled(v) { enabled = v; if (!v) flush(); else { Object.values(actors).forEach(place); kick(); } updateLag(); notify(); },
    setPaused(v) { paused = v; if (!v) kick(); updateLag(); notify(); },
    focusArea(id) { const a = layout.areas[id]; if (a) focusGrid(a.footprint[0] + a.footprint[2] / 2, a.footprint[1] + a.footprint[3] / 2, 720); },
    focusEmployee(id) { const a = actors[id]; if (a) focusGrid(a.pos[0], a.pos[1], 720); },
    setStatus(states) {
      for (const [id, a] of Object.entries(actors)) {
        const st = states[id] || {}, s = st.status;
        a.sdot.setAttribute('fill', st.paused ? '#5D6C8C' : s === 'WAITING_APPROVAL' ? '#F28C28' : s === 'WORKING' || s === 'QUEUED' ? '#4A79F0' : '#9AAAC8');
        if (st.paused && a.wait !== 'pause') { a.wait = 'pause'; if (!a.op) setBubble(a, 'pause'); }
        if (!st.paused && a.wait === 'pause') { a.wait = null; if (!a.op) setBubble(a, null); }
      }
    },
    setBeacons(pending) { svg.querySelectorAll('.beacon').forEach((b) => b.classList.toggle('on', pending)); },
    // restore positions for work already waiting on a person when the page loads
    placeAtArea(id, area, icon) { const a = actors[id]; if (!a || !layout.areas[area]) return; jump(a, { op: 'move', to: { area } }); a.wait = icon; setBubble(a, icon); place(a); sortDyn(); },
    whereIs(id) {
      const a = actors[id]; if (!a) return '';
      const name = (n) => { const ar = graph.nodeArea[n]; return ar ? layout.areas[ar].name : 'the road'; };
      if (!enabled) return `${name(a.node)} (latest known)`;
      if (a.op && a.op.op === 'move') return `Walking to ${name(a.op.target)}`;
      return `At ${name(a.node)}`;
    },
    highlight(kind, id) {
      svg.querySelectorAll('.bld.sel, .actor.sel').forEach((n) => n.classList.remove('sel'));
      if (kind === 'area') svg.querySelectorAll(`.bld[data-id="${CSS.escape(id)}"]`).forEach((n) => n.classList.add('sel'));
      if (kind === 'worker') { const n = dyn.querySelector(`[data-id="${CSS.escape(id)}"]`); if (n) n.classList.add('sel'); }
    },
  };
}
