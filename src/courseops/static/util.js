/* Shared helpers for the map client and the setup screen.
 *
 * Loaded as a plain script before app.js and setup.js, like icons.js: the
 * frontend has no build step on purpose, so "shared" means a global defined
 * in a file both pages include. Keep this file small and dependency-free -
 * anything here runs on a field phone before the map has drawn.
 */

/* Escapes quotes as well as angle brackets. The textContent->innerHTML trick
   does NOT escape " or ', which makes it unsafe the moment a value lands
   inside an attribute. Everything interpolated into markup goes through here.
   One copy: the map and setup each carried a character-for-character
   duplicate, and two copies of an escaper are two places to get it wrong. */
function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
