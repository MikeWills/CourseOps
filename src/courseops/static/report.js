/* The after-event report page. Two things, both done by the browser:
   clock times in the event's zone, and a small still map beside each note.

   A file rather than two inline scripts because the Content-Security-Policy
   allows no inline script, and the report is the one page that interpolates
   club-typed text (a course name, a zone) near a <script> block. Every value
   it needs comes off the document: the zone from #tz, the courses from the
   JSON block, the tile URL and zoom from <body data-*>. */

/* Stored UTC; shown in the event's zone. The browser has the zone database;
   a Windows install of Python may not. If the zone name is unknown, fall
   back to the reader's own zone and say so, rather than showing UTC without
   a label - a time with the wrong zone on it is worse than none. */
(function () {
  /* Read from the escaped element, never interpolated into a script: a
     zone name is admin-typed text, and text inside a <script> is the one
     place HTML escaping does not protect. */
  var tz = document.getElementById("tz").textContent;
  var opts = { hour: "2-digit", minute: "2-digit", hour12: false };
  var fmt;
  try { fmt = new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: tz }, opts)); }
  catch (err) {
    fmt = new Intl.DateTimeFormat(undefined, opts);
    document.getElementById("tz").textContent = "your local zone (event zone " + tz + " unknown)";
  }
  document.querySelectorAll("time[datetime]").forEach(function (el) {
    var d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d)) el.textContent = fmt.format(d);
  });
})();

/* A small, still map for each note: the course line for context and a dot
   where it happened. Not interactive - this page is printed or screenshotted,
   and a map that pans under a thumb is a map that shows the wrong corner.
   If Leaflet did not load, the box stays a plain grey square and the words
   beside it still say where. */
(function () {
  if (typeof L === "undefined") return;
  var tileUrl = document.body.dataset.tileUrl;
  var zoom = Number(document.body.dataset.miniZoom);
  var courses = JSON.parse(document.getElementById("courses").textContent || "[]");
  document.querySelectorAll(".mini[data-lat]").forEach(function (el) {
    var lat = Number(el.dataset.lat), lon = Number(el.dataset.lon);
    var map = L.map(el, { zoomControl: false, dragging: false, scrollWheelZoom: false,
      doubleClickZoom: false, touchZoom: false, boxZoom: false, keyboard: false,
      attributionControl: false }).setView([lat, lon], zoom);
    L.tileLayer(tileUrl, { maxZoom: 19 }).addTo(map);
    courses.forEach(function (c) {
      L.polyline(c.coordinates.map(function (p) { return [p[1], p[0]]; }),
        { color: c.color, weight: 4, opacity: 0.85 }).addTo(map);
    });
    L.marker([lat, lon], { icon: L.divIcon({ className: "", iconSize: [14, 14],
      iconAnchor: [7, 7], html: '<div class="dot"></div>' }), interactive: false }).addTo(map);
  });
})();
