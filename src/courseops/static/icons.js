/* Shared glyph set for map layers, used by both the map and the setup screen.
 *
 * Inline SVG here because these particular icons take a colour: a club sets a
 * layer's colour and the marker follows it, which a glyph cannot do. Glyphs are
 * a fine choice elsewhere in the app - but check one in Chrome on Windows
 * first, where U+270E with the U+FE0E text-presentation selector still rendered
 * as a full-colour emoji pencil, ignoring `color` and reading as decoration.
 * An icon font is another file to ship and to fail to load, and the frontend
 * has no build step on purpose.
 *
 * Every path is drawn on a 16x16 box with `currentColor`, so an icon takes the
 * colour of whatever it sits in - which is what lets a club set a layer colour
 * and have the marker follow it.
 *
 * This list is a palette, not a taxonomy. A club names its own layers; these
 * are only the shapes available to put on them. Adding one is a line here.
 */

const POI_GLYPHS = {
  pin:      ['Pin',            'M8 14.2s4.6-4.4 4.6-7.4a4.6 4.6 0 1 0-9.2 0C3.4 9.8 8 14.2 8 14.2z|c:8,6.8,1.7'],
  cup:      ['Cup',            'M4.4 4.4h7.2l-.8 8.1a1.4 1.4 0 0 1-1.4 1.2H6.6a1.4 1.4 0 0 1-1.4-1.2z|M4.7 7.4h6.6'],
  drop:     ['Water',          'M8 2.4s4 4.8 4 7.3a4 4 0 0 1-8 0c0-2.5 4-7.3 4-7.3z'],
  cross:    ['Medical',        'M6.4 2.6h3.2v3.8h3.8v3.2H9.6v3.8H6.4V9.6H2.6V6.4h3.8z'],
  kit:      ['First aid kit',  'r:2.4,4.6,11.2,7.8,1.4|M6.6 6.2h2.8|M8 6.2v4.6|M5.7 8.5h4.6'],
  flag:     ['Flag',           'M4.2 13.6V2.4|M4.2 3.1h7.6l-1.6 2.5 1.6 2.5H4.2'],
  marker:   ['Mile marker',    'r:3.6,2.4,8.8,4.4,0.8|M8 6.8v6.4|M5.6 13.2h4.8'],
  cone:     ['Traffic cone',   'M8 2.6l4.2 9.4H3.8z|M2.6 13.4h10.8'],
  car:      ['Parking',        'M3.2 10.4h9.6|M4.4 10.4l1.1-3.2a1.5 1.5 0 0 1 1.4-1h2.2a1.5 1.5 0 0 1 1.4 1l1.1 3.2|c:5.2,11.8,1|c:10.8,11.8,1'],
  bus:      ['Shuttle',        'r:2.8,2.8,10.4,8.6,1.4|M2.8 7.6h10.4|c:5.4,12.6,0.9|c:10.6,12.6,0.9'],
  bike:     ['Bike',           'c:4.2,11,2.6|c:11.8,11,2.6|M4.2 11l3-5.4h3.2|M7.2 11l3.4-5.4'],
  toilet:   ['Toilet',         'r:3.4,2.4,9.2,11.2,1|c:10.4,8,0.8'],
  radio:    ['Radio',          'c:8,10.4,1.6|M8 8.8V4.4|M5.6 4.4a3.4 3.4 0 0 1 4.8 0|M4 2.8a5.6 5.6 0 0 1 8 0'],
  food:     ['Food',           'M5.4 2.6v4a1.4 1.4 0 0 1-2.8 0v-4|M4 6.6v6.8|M11.4 2.6c1.4 1 1.4 4.2 0 5.2v5.6'],
  tent:     ['Shelter',        'M2.6 7.6L8 3l5.4 4.6|M4.2 8.6v4.8h7.6V8.6|M6.8 13.4V10h2.4v3.4'],
  info:     ['Information',    'c:8,8,5.4|M8 7.4v3.8|c:8,5.2,0.5'],
  warning:  ['Hazard',         'M8 2.6l5.8 10.8H2.2z|M8 6.8v3|c:8,11.8,0.5'],
  star:     ['Landmark',       'M8 2.4l1.7 3.6 3.9.5-2.9 2.8.7 3.9L8 11.3l-3.4 1.9.7-3.9-2.9-2.8 3.9-.5z'],
  camera:   ['Photo point',    'r:2.4,5,11.2,8,1.4|c:8,9,2.3|M6.2 5l0.9-1.5h1.8L9.8 5'],
  people:   ['Spectators',     'c:5.4,5.4,2|c:10.8,6,1.6|M2.4 13.2a3.2 3.2 0 0 1 6 0|M9 13.2a2.7 2.7 0 0 1 4.6-1.9'],
  finish:   ['Finish',         'M4.2 13.6V2.4|r:4.2,3,7.4,5,0|M7.9 3v5|M4.2 5.5h7.4'],
  timing:   ['Timing',         'c:8,8.8,4.8|M8 6v2.8l2 1.4|M6 2.6h4'],
};

/* Turn one palette entry into SVG markup.
 *
 * The compact notation exists so the table above stays readable at a glance:
 *   `c:cx,cy,r`            a circle
 *   `r:x,y,w,h,rx`         a rectangle
 *   anything else          a path `d`
 * Parts are separated by `|` and drawn in order.
 */
function glyphSvg(name, size) {
  const entry = POI_GLYPHS[name] || POI_GLYPHS.pin;
  const shapes = entry[1].split('|').map((part) => {
    if (part.startsWith('c:')) {
      const [cx, cy, r] = part.slice(2).split(',');
      return `<circle cx="${cx}" cy="${cy}" r="${r}"/>`;
    }
    if (part.startsWith('r:')) {
      const [x, y, w, h, rx] = part.slice(2).split(',');
      return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}"/>`;
    }
    return `<path d="${part}"/>`;
  }).join('');

  const px = size || 16;
  return `<svg viewBox="0 0 16 16" width="${px}" height="${px}" aria-hidden="true"`
    + ' fill="none" stroke="currentColor" stroke-width="1.4"'
    + ` stroke-linecap="round" stroke-linejoin="round">${shapes}</svg>`;
}

function glyphLabel(name) {
  const entry = POI_GLYPHS[name];
  return entry ? entry[0] : 'Pin';
}
